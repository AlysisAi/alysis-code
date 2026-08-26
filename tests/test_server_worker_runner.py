from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import alysis_code.server.worker_runner as worker_runner_mod
from alysis_code.server.settings import ServerSettings
from alysis_code.server.store import ServerStore
from alysis_code.server.worker_runner import (
    BwrapProcessRunner,
    DockerProcessRunner,
    JobRunner,
    JobState,
)


def _settings(tmp_path: Path, *, worker_backend: str = "bwrap") -> ServerSettings:
    return ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data",
        token=None,
        max_upload_bytes=2 * 1024 * 1024,
        max_concurrent_jobs=1,
        worker_backend=worker_backend,
        worker_sandbox_mode="strict",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_base_url=False,
        allow_client_model=True,
    )


def _settings_with_concurrency(
    tmp_path: Path,
    *,
    max_concurrent_jobs: int,
    worker_backend: str = "bwrap",
) -> ServerSettings:
    return ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data",
        token=None,
        max_upload_bytes=2 * 1024 * 1024,
        max_concurrent_jobs=max_concurrent_jobs,
        worker_backend=worker_backend,
        worker_sandbox_mode="strict",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_base_url=False,
        allow_client_model=True,
    )


class _DummyPopen:
    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.stdout = iter(["line1\n", "line2\n"])

    def wait(self) -> int:
        return 0

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None


class _BlockingPopen:
    def __init__(self, name: str, *, release_event: threading.Event) -> None:
        self.args = [name]
        self.stdout = iter([f"{name}\n"])
        self._release_event = release_event
        self._terminated = False
        self.terminate_calls = 0

    def wait(self) -> int:
        self._release_event.wait(timeout=2.0)
        return 143 if self._terminated else 0

    def poll(self) -> int | None:
        if self._terminated:
            return 143
        if self._release_event.is_set():
            return 0
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._terminated = True
        self._release_event.set()


class _SequencedRunner:
    def __init__(self, procs: list[object]) -> None:
        self._procs = list(procs)
        self.spawn_calls: list[dict[str, object]] = []

    def spawn(
        self,
        *,
        workspace: Path,
        job_dir: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> object:
        self.spawn_calls.append(
            {
                "workspace": workspace,
                "job_dir": job_dir,
                "argv": list(argv),
                "env": dict(env),
            }
        )
        if not self._procs:
            raise AssertionError("unexpected spawn without queued fake process")
        return self._procs.pop(0)


def _wait_for_status(
    runner: JobRunner,
    job_id: str,
    expected_status: str,
    *,
    timeout_s: float = 2.0,
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if runner.get_status(job_id).status == expected_status:
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach status {expected_status!r}")


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_bwrap_runner_mounts_job_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(worker_runner_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_runner_mod, "shutil_which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(worker_runner_mod, "_supports_bwrap_unshare_cgroup", lambda: False)
    monkeypatch.setattr(worker_runner_mod, "_runtime_roots", lambda: [])

    def fake_popen(args, **_kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return _DummyPopen(args)

    monkeypatch.setattr(worker_runner_mod.subprocess, "Popen", fake_popen)

    runner = BwrapProcessRunner(network="on")
    runner.spawn(
        workspace=workspace,
        job_dir=job_dir,
        argv=["alysis", "--help"],
        env={"FOO": "bar"},
    )
    argv = list(captured["args"])
    bind_idx = argv.index("--bind")
    assert argv[bind_idx + 1] == os.fspath(workspace.resolve())
    assert argv[bind_idx + 2] == "/workspace"
    job_bind_idx = argv.index("--bind", bind_idx + 1)
    assert argv[job_bind_idx + 1] == os.fspath(job_dir.resolve())
    assert argv[job_bind_idx + 2] == "/alysis_job"
    assert "--setenv" in argv


def test_docker_runner_mounts_job_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(worker_runner_mod, "shutil_which", lambda _name: "/usr/bin/docker")

    def fake_popen(args, **_kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        return _DummyPopen(args)

    monkeypatch.setattr(worker_runner_mod.subprocess, "Popen", fake_popen)

    image = "test/alysis-sandbox:server"
    runner = DockerProcessRunner(image=image, network="on")
    runner.spawn(
        workspace=workspace,
        job_dir=job_dir,
        argv=["alysis", "--help"],
        env={"FOO": "bar"},
    )
    argv = list(captured["args"])
    job_mount = f"{os.fspath(job_dir.resolve())}:/alysis_job:rw"
    assert job_mount in argv
    assert image in argv


class _CapturingRunner:
    def __init__(self) -> None:
        self.last_workspace: Path | None = None
        self.last_job_dir: Path | None = None
        self.last_env: dict[str, str] | None = None
        self.last_argv: list[str] | None = None

    def spawn(
        self,
        *,
        workspace: Path,
        job_dir: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> _DummyPopen:
        self.last_workspace = workspace
        self.last_job_dir = job_dir
        self.last_env = dict(env)
        self.last_argv = list(argv)
        return _DummyPopen(argv)


def test_job_runner_maps_config_and_data_to_alysis_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, worker_backend="bwrap")
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    job_paths = store.create_job_paths(run_id, "job_test")

    state = JobState(
        job_id="job_test",
        run_id=run_id,
        status="queued",
        command=["alysis", "run", "--help"],
        created_at="2026-02-22T00:00:00+00:00",
        logs_path=os.fspath(job_paths.logs_path),
    )
    runner = JobRunner(settings, store)
    fake_runner = _CapturingRunner()
    monkeypatch.setattr(runner, "_outer_runner", lambda: fake_runner)

    runner._run_job_inner(state, run_paths, job_paths)

    assert (job_paths.job_dir / "config").is_dir()
    assert (job_paths.job_dir / "data").is_dir()
    assert fake_runner.last_workspace == run_paths.workspace_dir
    assert fake_runner.last_job_dir == job_paths.job_dir
    assert fake_runner.last_env is not None
    assert fake_runner.last_env["ALYSIS_CONFIG_DIR"] == "/alysis_job/config"
    assert fake_runner.last_env["ALYSIS_DATA_DIR"] == "/alysis_job/data"
    assert fake_runner.last_env["HOME"] == "/tmp"
    assert fake_runner.last_env["ALYSIS_SHELL_SANDBOX_BACKEND"] == "bwrap"
    assert fake_runner.last_env["ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META"] == "1"
    assert state.status == "succeeded"
    assert state.exit_code == 0
    assert job_paths.logs_path.exists()
    assert "line1" in job_paths.logs_path.read_text(encoding="utf-8")
    runner.close()


def test_job_runner_uses_default_linux_path_for_docker_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, worker_backend="docker")
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    job_paths = store.create_job_paths(run_id, "job_docker")

    state = JobState(
        job_id="job_docker",
        run_id=run_id,
        status="queued",
        command=["alysis", "run", "--help"],
        created_at="2026-02-22T00:00:00+00:00",
        logs_path=os.fspath(job_paths.logs_path),
    )
    runner = JobRunner(settings, store)
    fake_runner = _CapturingRunner()
    monkeypatch.setattr(runner, "_outer_runner", lambda: fake_runner)
    monkeypatch.setenv("PATH", r"C:\\Windows\\System32;C:\\Program Files\\Git\\bin")

    runner._run_job_inner(state, run_paths, job_paths)

    assert fake_runner.last_env is not None
    assert fake_runner.last_env["PATH"] == worker_runner_mod._DEFAULT_PATH
    runner.close()


def test_job_runner_sets_strict_inner_shell_sandbox_for_docker_workers(tmp_path: Path) -> None:
    settings = _settings(tmp_path, worker_backend="docker")
    store = ServerStore(settings)
    runner = JobRunner(settings, store)

    env = runner._build_worker_env()

    assert env["ALYSIS_SHELL_SANDBOX_MODE"] == "strict"
    assert env["ALYSIS_VERIFY_SANDBOX_MODE"] == "strict"
    runner.close()


def test_job_runner_uses_bounded_worker_pool_for_queued_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_concurrency(tmp_path, max_concurrent_jobs=1)
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    release_first = threading.Event()
    fake_runner = _SequencedRunner(
        [_BlockingPopen("job-1", release_event=release_first), _DummyPopen(["job-2"])]
        + [_DummyPopen(["job-extra"]) for _ in range(4)]
    )
    runner = JobRunner(settings, store)
    monkeypatch.setattr(runner, "_outer_runner", lambda: fake_runner)

    try:
        first_job = runner.start_job(run_id=run_id, command=["alysis", "run", "job-1"])
        _wait_for_status(runner, first_job, "running")

        queued_jobs = [
            runner.start_job(run_id=run_id, command=["alysis", "run", f"job-{idx}"])
            for idx in range(2, 7)
        ]

        assert len(runner._worker_threads) == 1
        assert [thread.name for thread in runner._worker_threads] == ["alysis-server-worker-1"]
        assert len(fake_runner.spawn_calls) == 1
        assert all(runner.get_status(job_id).status == "queued" for job_id in queued_jobs)

        release_first.set()
        _wait_for_status(runner, first_job, "succeeded")
        for job_id in queued_jobs:
            _wait_for_status(runner, job_id, "succeeded")
    finally:
        release_first.set()
        runner.close()


def test_queued_jobs_execute_and_persist_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_concurrency(tmp_path, max_concurrent_jobs=1)
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    release_first = threading.Event()
    fake_runner = _SequencedRunner(
        [
            _BlockingPopen("job-1", release_event=release_first),
            _DummyPopen(["job-2"]),
        ]
    )
    runner = JobRunner(settings, store)
    monkeypatch.setattr(runner, "_outer_runner", lambda: fake_runner)

    try:
        first_job = runner.start_job(run_id=run_id, command=["alysis", "run", "job-1"])
        _wait_for_status(runner, first_job, "running")

        second_job = runner.start_job(run_id=run_id, command=["alysis", "run", "job-2"])
        second_job_dir = run_paths.jobs_dir / second_job
        second_meta = _read_json(second_job_dir / "meta.json")
        assert second_meta["status"] == "queued"
        assert second_meta["started_at"] is None
        assert (second_job_dir / "result.json").exists() is False
        assert runner.read_logs(second_job) == ""

        release_first.set()
        _wait_for_status(runner, first_job, "succeeded")
        _wait_for_status(runner, second_job, "succeeded")

        second_meta = _read_json(second_job_dir / "meta.json")
        second_result = _read_json(second_job_dir / "result.json")
        assert second_meta["status"] == "succeeded"
        assert second_meta["started_at"] is not None
        assert second_meta["finished_at"] is not None
        assert second_result["status"] == "succeeded"
        assert second_result["finished_at"] is not None
        assert "starting job" in runner.read_logs(second_job)
    finally:
        release_first.set()
        runner.close()


def test_cancel_queued_job_marks_terminal_and_skips_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_concurrency(tmp_path, max_concurrent_jobs=1)
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    release_first = threading.Event()
    fake_runner = _SequencedRunner(
        [
            _BlockingPopen("job-1", release_event=release_first),
            _DummyPopen(["job-2"]),
        ]
    )
    runner = JobRunner(settings, store)
    monkeypatch.setattr(runner, "_outer_runner", lambda: fake_runner)

    try:
        first_job = runner.start_job(run_id=run_id, command=["alysis", "run", "job-1"])
        _wait_for_status(runner, first_job, "running")

        queued_job = runner.start_job(run_id=run_id, command=["alysis", "run", "job-2"])
        queued_job_dir = run_paths.jobs_dir / queued_job
        runner.cancel_job(queued_job)

        _wait_for_status(runner, queued_job, "cancelled")
        queued_meta = _read_json(queued_job_dir / "meta.json")
        queued_result = _read_json(queued_job_dir / "result.json")
        assert queued_meta["status"] == "cancelled"
        assert queued_meta["started_at"] is None
        assert queued_meta["finished_at"] is not None
        assert queued_result["status"] == "cancelled"
        assert (queued_job_dir / "config").exists() is False
        assert (queued_job_dir / "data").exists() is False
        assert queued_job_dir.joinpath("logs.txt").exists() is False

        release_first.set()
        _wait_for_status(runner, first_job, "succeeded")
        time.sleep(0.05)
        assert len(fake_runner.spawn_calls) == 1
    finally:
        release_first.set()
        runner.close()


def test_cancel_running_job_terminates_and_ends_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_concurrency(tmp_path, max_concurrent_jobs=1)
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    release_event = threading.Event()
    proc = _BlockingPopen("job-1", release_event=release_event)
    fake_runner = _SequencedRunner([proc])
    runner = JobRunner(settings, store)
    monkeypatch.setattr(runner, "_outer_runner", lambda: fake_runner)

    try:
        job_id = runner.start_job(run_id=run_id, command=["alysis", "run", "job-1"])
        _wait_for_status(runner, job_id, "running")

        runner.cancel_job(job_id)
        _wait_for_status(runner, job_id, "cancelled")

        job_dir = run_paths.jobs_dir / job_id
        meta = _read_json(job_dir / "meta.json")
        result = _read_json(job_dir / "result.json")
        assert proc.terminate_calls == 1
        assert meta["status"] == "cancelled"
        assert result["status"] == "cancelled"
        assert "starting job" in runner.read_logs(job_id)
    finally:
        release_event.set()
        runner.close()
