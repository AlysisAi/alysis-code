from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import alysis_code.terminal_manager as terminal_manager_mod
from alysis_code.background_runner import (
    BackgroundProcessSpawn,
    DisabledBackgroundRunner,
    HostBackgroundRunner,
)
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.terminal_manager import (
    BackgroundProcess,
    TerminalLimitError,
    TerminalManager,
)


class FailingBackgroundRunner:
    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        raise RuntimeError("spawn failed")


class CleanupCountingRunner:
    def __init__(self) -> None:
        self.count = 0

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        spawn = HostBackgroundRunner().start(
            root=root,
            cwd=cwd,
            cmd=cmd,
            env_overrides=env_overrides,
        )

        def cleanup() -> None:
            self.count += 1
            spawn.cleanup()

        return BackgroundProcessSpawn(
            popen=spawn.popen,
            cleanup=cleanup,
            started_argv=spawn.started_argv,
        )


class BlockingRunner:
    def __init__(self, release: threading.Event) -> None:
        self.entered = threading.Event()
        self.release = release

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        self.entered.set()
        if not self.release.wait(2.0):
            raise RuntimeError("blocking runner was not released")
        return HostBackgroundRunner().start(
            root=root,
            cwd=cwd,
            cmd=cmd,
            env_overrides=env_overrides,
        )


class FakePopen:
    def __init__(self, *, stdout, stderr, returncode: int = 0, pid: int = 0) -> None:  # type: ignore[no-untyped-def]
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self.pid = pid
        self._returncode = returncode
        self.wait_called = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.signals: list[int] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_called.set()
        self.returncode = self._returncode
        return self._returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        raise AssertionError("process-group cleanup should not call terminate fallback")

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)


class TaskkillFallbackPopen:
    pid = 12345

    def __init__(self) -> None:
        self.kill_calls = 0

    def kill(self) -> None:
        self.kill_calls += 1


def test_windows_process_tree_kill_uses_taskkill(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    popen = TaskkillFallbackPopen()
    monkeypatch.setattr(terminal_manager_mod.subprocess, "run", fake_run)

    terminal_manager_mod._kill_windows_process_tree(popen)  # type: ignore[arg-type]

    assert calls[0][0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert calls[0][1]["timeout"] == 5.0
    assert popen.kill_calls == 0


def test_windows_process_tree_kill_falls_back_to_popen_kill(monkeypatch) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("taskkill unavailable")

    popen = TaskkillFallbackPopen()
    monkeypatch.setattr(terminal_manager_mod.subprocess, "run", fail_run)

    terminal_manager_mod._kill_windows_process_tree(popen)  # type: ignore[arg-type]

    assert popen.kill_calls == 1


class DirectBlockingPopen:
    def __init__(self, *, pid: int = 123456) -> None:
        self.stdout = None
        self.stderr = None
        self.returncode: int | None = None
        self.pid = pid
        self.wait_called = threading.Event()
        self._exited = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_called.set()
        if timeout is None:
            self._exited.wait()
        elif not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="direct-fake", timeout=timeout)
        return self.returncode if self.returncode is not None else 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._exited.set()


class SpawnThenFailRunner:
    def __init__(self, popen: FakePopen) -> None:
        self.popen = popen
        self.cleanup_calls = 0

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        return BackgroundProcessSpawn(
            popen=self.popen,  # type: ignore[arg-type]
            cleanup=self._cleanup,
            started_argv=(cmd,),
        )

    def _cleanup(self) -> None:
        self.cleanup_calls += 1


class DirectModeRunner:
    def __init__(self, popen: DirectBlockingPopen) -> None:
        self.popen = popen
        self.cleanup_calls = 0

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        return BackgroundProcessSpawn(
            popen=self.popen,  # type: ignore[arg-type]
            cleanup=self._cleanup,
            started_argv=(cmd,),
            termination_mode="direct",
        )

    def _cleanup(self) -> None:
        self.cleanup_calls += 1


def _settings(**overrides: object) -> ShellSandboxSettings:
    return replace(ShellSandboxSettings(), **overrides)


def _manager(settings: ShellSandboxSettings | None = None) -> TerminalManager:
    return TerminalManager(
        runner=HostBackgroundRunner(),
        settings=settings or ShellSandboxSettings(),
    )


def _shell_join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _python_cmd(code: str) -> str:
    return _shell_join([sys.executable, "-c", code])


def _process(manager: TerminalManager, process_id: str) -> BackgroundProcess:
    return manager._processes[process_id]


def _wait_for_exit(manager: TerminalManager, process_id: str, timeout_s: float = 5.0) -> None:
    assert _process(manager, process_id).wait_for_exit(timeout_s)


def _texts(manager: TerminalManager, process_id: str) -> list[str]:
    return [line.text.rstrip("\r\n") for line in manager.read(process_id).lines]


def test_start_runs_command_and_captures_stdout(tmp_path: Path) -> None:
    manager = _manager()
    process_id = manager.start(
        cmd=_python_cmd("print('hello'); print('world')"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        _wait_for_exit(manager, process_id)
        snapshot = manager.read(process_id)

        assert [line.text.rstrip("\r\n") for line in snapshot.lines] == ["hello", "world"]
        assert snapshot.exit_code == 0
        assert snapshot.status == "exited"
    finally:
        manager.shutdown_all()


def test_stderr_captured_separately(tmp_path: Path) -> None:
    manager = _manager()
    process_id = manager.start(
        cmd=_python_cmd("import sys; sys.stderr.write('err1\\n')"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        _wait_for_exit(manager, process_id)
        snapshot = manager.read(process_id)

        assert [(line.stream, line.text.rstrip("\r\n")) for line in snapshot.lines] == [
            ("stderr", "err1")
        ]
    finally:
        manager.shutdown_all()


def test_read_with_since_returns_only_new_lines(tmp_path: Path) -> None:
    manager = _manager()
    process_id = manager.start(
        cmd=_python_cmd("print('hello')"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        _wait_for_exit(manager, process_id)
        first = manager.read(process_id, since=0)
        second = manager.read(process_id, since=first.next_seq)

        assert len(first.lines) == 1
        assert second.lines == ()
    finally:
        manager.shutdown_all()


def test_long_running_process_is_killable(tmp_path: Path) -> None:
    manager = _manager(_settings(background_kill_timeout_s=0.2))
    process_id = manager.start(
        cmd=_python_cmd("import time; time.sleep(60)"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        started = time.perf_counter()
        snapshot = manager.kill(process_id)
        elapsed = time.perf_counter() - started

        assert elapsed < 2.0
        assert snapshot.status == "killed"
        assert snapshot.exit_code is not None
        assert snapshot.exit_code != 0
    finally:
        manager.shutdown_all()


def test_kill_is_idempotent(tmp_path: Path) -> None:
    manager = _manager(_settings(background_kill_timeout_s=0.2))
    process_id = manager.start(
        cmd=_python_cmd("import time; time.sleep(60)"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        first = manager.kill(process_id)
        second = manager.kill(process_id)

        assert first.status == "killed"
        assert second.status == first.status
        assert second.exit_code == first.exit_code
    finally:
        manager.shutdown_all()


def test_concurrent_limit_enforced(tmp_path: Path) -> None:
    manager = _manager(_settings(background_max_concurrent=2, background_kill_timeout_s=0.2))
    first = manager.start(
        cmd=_python_cmd("import time; time.sleep(60)"),
        cwd=tmp_path,
        root=tmp_path,
    )
    second = manager.start(
        cmd=_python_cmd("import time; time.sleep(60)"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        with pytest.raises(TerminalLimitError):
            manager.start(
                cmd=_python_cmd("import time; time.sleep(60)"),
                cwd=tmp_path,
                root=tmp_path,
            )

        manager.kill(first)
        third = manager.start(
            cmd=_python_cmd("import time; time.sleep(60)"),
            cwd=tmp_path,
            root=tmp_path,
        )

        assert third not in {first, second}
    finally:
        manager.shutdown_all()


def test_buffer_caps_drop_oldest(tmp_path: Path) -> None:
    manager = _manager(_settings(background_output_max_lines=10))
    process_id = manager.start(
        cmd=_python_cmd("for i in range(50): print(f'line-{i}')"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        _wait_for_exit(manager, process_id)
        snapshot = manager.read(process_id)

        assert snapshot.dropped_lines >= 40
        assert [line.text.rstrip("\r\n") for line in snapshot.lines] == [
            f"line-{idx}" for idx in range(40, 50)
        ]
    finally:
        manager.shutdown_all()


def test_partial_lines_decoded_correctly(tmp_path: Path) -> None:
    manager = _manager()
    process_id = manager.start(
        cmd=_python_cmd("import sys; sys.stdout.write('abc'); sys.stdout.flush()"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        _wait_for_exit(manager, process_id)

        assert _texts(manager, process_id) == ["abc"]
    finally:
        manager.shutdown_all()


def test_failed_to_start_does_not_leak_registry(tmp_path: Path) -> None:
    manager = TerminalManager(
        runner=FailingBackgroundRunner(),
        settings=ShellSandboxSettings(),
    )

    with pytest.raises(RuntimeError, match="spawn failed"):
        manager.start(cmd="echo hi", cwd=tmp_path, root=tmp_path)

    assert manager.list() == ()


def test_start_does_not_block_other_operations_during_slow_runner_start(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    runner = BlockingRunner(release)
    manager = TerminalManager(runner=runner, settings=ShellSandboxSettings())
    process_ids: list[str] = []
    errors: list[Exception] = []

    def start_process() -> None:
        try:
            process_ids.append(
                manager.start(
                    cmd=_python_cmd("import time; time.sleep(60)"),
                    cwd=tmp_path,
                    root=tmp_path,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=start_process)
    thread.start()
    try:
        assert runner.entered.wait(1.0)

        started = time.perf_counter()
        assert manager.list() == ()
        elapsed = time.perf_counter() - started
        assert elapsed < 0.1

        release.set()
        thread.join(2.0)
        assert not thread.is_alive()
        assert errors == []
        assert len(process_ids) == 1
        snapshot = manager.read(process_ids[0])
        assert snapshot.process_id == process_ids[0]
    finally:
        release.set()
        thread.join(2.0)
        manager.shutdown_all()


def test_concurrent_start_respects_pending_reservation(tmp_path: Path) -> None:
    release = threading.Event()
    runner = BlockingRunner(release)
    manager = TerminalManager(
        runner=runner,
        settings=_settings(background_max_concurrent=1),
    )
    process_ids: list[str] = []
    errors: list[Exception] = []

    def start_first_process() -> None:
        try:
            process_ids.append(
                manager.start(
                    cmd=_python_cmd("import time; time.sleep(60)"),
                    cwd=tmp_path,
                    root=tmp_path,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=start_first_process)
    thread.start()
    try:
        assert runner.entered.wait(1.0)
        with pytest.raises(TerminalLimitError):
            manager.start(
                cmd=_python_cmd("import time; time.sleep(60)"),
                cwd=tmp_path,
                root=tmp_path,
            )

        release.set()
        thread.join(2.0)
        assert not thread.is_alive()
        assert errors == []
        assert len(process_ids) == 1
        assert manager.read(process_ids[0]).process_id == process_ids[0]
    finally:
        release.set()
        thread.join(2.0)
        manager.shutdown_all()


def test_spawn_wrap_failure_uses_process_group_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    popen = FakePopen(stdout=None, stderr=None, pid=123456)
    runner = SpawnThenFailRunner(popen)
    manager = TerminalManager(
        runner=runner,  # type: ignore[arg-type]
        settings=_settings(background_output_max_lines=0),
    )
    sent_signals: list[tuple[int, int]] = []

    if os.name != "nt":
        monkeypatch.setattr(
            terminal_manager_mod,
            "_signal_posix_process_group",
            lambda pgid, signum: sent_signals.append((pgid, signum)) or True,
        )

    with pytest.raises(ValueError, match="max_lines must be positive"):
        manager.start(cmd="echo hi", cwd=tmp_path, root=tmp_path)

    assert manager.list() == ()
    assert runner.cleanup_calls == 1
    if os.name == "nt":
        assert popen.signals == [signal.CTRL_BREAK_EVENT]
    else:
        assert sent_signals == [(123456, signal.SIGTERM)]
        assert popen.terminate_calls == 0


def test_direct_termination_kill_uses_cleanup_and_direct_process_terminate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    popen = DirectBlockingPopen()
    runner = DirectModeRunner(popen)
    manager = TerminalManager(
        runner=runner,  # type: ignore[arg-type]
        settings=_settings(background_kill_timeout_s=0.5),
    )

    if os.name != "nt":
        monkeypatch.setattr(
            terminal_manager_mod,
            "_signal_posix_process_group",
            lambda _pgid, _signum: pytest.fail(
                "direct termination must not signal a process group"
            ),
        )

    process_id = manager.start(cmd="direct fake", cwd=tmp_path, root=tmp_path)
    try:
        assert popen.wait_called.wait(1.0)

        snapshot = manager.kill(process_id)

        assert snapshot.status == "killed"
        assert runner.cleanup_calls == 1
        assert popen.terminate_calls == 1
        assert popen.kill_calls == 0
    finally:
        manager.shutdown_all()


def test_direct_termination_spawn_wrap_failure_does_not_signal_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    popen = DirectBlockingPopen()
    runner = DirectModeRunner(popen)
    manager = TerminalManager(
        runner=runner,  # type: ignore[arg-type]
        settings=_settings(background_output_max_lines=0),
    )

    if os.name != "nt":
        monkeypatch.setattr(
            terminal_manager_mod,
            "_signal_posix_process_group",
            lambda _pgid, _signum: pytest.fail("direct cleanup must not signal a process group"),
        )

    with pytest.raises(ValueError, match="max_lines must be positive"):
        manager.start(cmd="direct fake", cwd=tmp_path, root=tmp_path)

    assert manager.list() == ()
    assert runner.cleanup_calls == 1
    assert popen.terminate_calls == 1
    assert popen.kill_calls == 0


def test_wait_for_exit_waits_until_output_readers_drain() -> None:
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    os.close(stderr_w)
    cleanup_called = threading.Event()
    stdout_file = os.fdopen(stdout_r, "rb", buffering=0)
    stderr_file = os.fdopen(stderr_r, "rb", buffering=0)
    popen = FakePopen(stdout=stdout_file, stderr=stderr_file)
    process = BackgroundProcess(
        spawn=BackgroundProcessSpawn(
            popen=popen,  # type: ignore[arg-type]
            cleanup=cleanup_called.set,
            started_argv=("fake",),
        ),
        cmd="fake",
        cwd=Path.cwd(),
        output_max_lines=10,
        output_max_bytes=1024,
    )
    try:
        assert popen.wait_called.wait(1.0)
        assert not process.wait_for_exit(0)

        os.write(stdout_w, b"tail\n")
        os.close(stdout_w)
        stdout_w = -1

        assert process.wait_for_exit(1.0)
        assert cleanup_called.is_set()
        snapshot = process.read()
        assert snapshot.status == "exited"
        assert [line.text for line in snapshot.lines] == ["tail\n"]
    finally:
        if stdout_w >= 0:
            os.close(stdout_w)


def test_cleanup_called_exactly_once_on_exit_and_repeated_kill(tmp_path: Path) -> None:
    runner = CleanupCountingRunner()
    manager = TerminalManager(runner=runner, settings=ShellSandboxSettings())
    process_id = manager.start(
        cmd=_python_cmd("print('done')"),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        _wait_for_exit(manager, process_id)
        assert runner.count == 1

        manager.kill(process_id)
        manager.kill(process_id)
        manager.shutdown_all()

        assert runner.count == 1
    finally:
        manager.shutdown_all()


def test_byte_cap_drops_oldest(tmp_path: Path) -> None:
    manager = _manager(_settings(background_output_max_lines=20, background_output_max_bytes=9))
    process_id = manager.start(
        cmd=_python_cmd(
            "import sys; "
            "[sys.stdout.buffer.write(f'x{i}\\n'.encode()) for i in range(10)]; "
            "sys.stdout.flush()"
        ),
        cwd=tmp_path,
        root=tmp_path,
    )
    try:
        _wait_for_exit(manager, process_id)
        snapshot = manager.read(process_id)

        assert snapshot.dropped_lines == 7
        assert [line.text for line in snapshot.lines] == ["x7\n", "x8\n", "x9\n"]
    finally:
        manager.shutdown_all()


def test_shutdown_all_kills_running_and_is_idempotent(tmp_path: Path) -> None:
    manager = _manager(_settings(background_kill_timeout_s=0.2))
    for _idx in range(3):
        manager.start(
            cmd=_python_cmd("import time; time.sleep(60)"),
            cwd=tmp_path,
            root=tmp_path,
        )
    try:
        manager.shutdown_all(kill_timeout_s=0.2)

        assert {summary.status for summary in manager.list()} == {"killed"}
        manager.shutdown_all(kill_timeout_s=0.2)
        manager.prune(older_than_s=0)
        assert manager.list() == ()
    finally:
        manager.shutdown_all()


def test_cwd_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    manager = _manager()

    with pytest.raises(ValueError, match="cwd escapes root"):
        manager.start(cmd="echo hi", cwd=outside, root=root)


def test_disabled_runner_raises_at_start(tmp_path: Path) -> None:
    manager = TerminalManager(
        runner=DisabledBackgroundRunner(),
        settings=ShellSandboxSettings(),
    )

    with pytest.raises(RuntimeError, match="disabled"):
        manager.start(cmd="echo hi", cwd=tmp_path, root=tmp_path)
