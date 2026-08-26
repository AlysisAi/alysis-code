from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_runtimes import RuntimeTurnRequest
from alysis_code.agent_runtimes.codex_cli import (
    CodexCliRuntimeAdapter,
    _runtime_environment,
    _terminate_process_tree,
)
from alysis_code.config import AgentRuntimeSettings

_THREAD_ID = "123e4567-e89b-12d3-a456-426614174000"
_OTHER_THREAD_ID = "123e4567-e89b-12d3-a456-426614174001"


class _SuccessfulPopen:
    instances: list[_SuccessfulPopen] = []

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode: int | None = None
        self.input: str | None = None
        self.timeout: float | None = None
        self.__class__.instances.append(self)

    def communicate(
        self,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        self.input = input
        self.timeout = timeout
        self.returncode = 0
        output_path = Path(self.command[self.command.index("--output-last-message") + 1])
        output_path.write_text("captured final message\n", encoding="utf-8")
        events = [
            {"type": "thread.started", "thread_id": _THREAD_ID},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "event final message"},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 40, "output_tokens": 8},
            },
        ]
        stdout = "\n".join(json.dumps(event) for event in events) + "\nnot-json\n"
        return stdout, "progress stays on stderr\n"

    def poll(self) -> int | None:
        return self.returncode


class _TimeoutPopen:
    instances: list[_TimeoutPopen] = []

    def __init__(self, command: list[str], **kwargs: Any) -> None:
        self.command = list(command)
        self.kwargs = kwargs
        self.pid = 9876
        self.returncode: int | None = None
        self.communicate_calls = 0
        self.__class__.instances.append(self)

    def communicate(
        self,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        del input
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            partial = json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "partial final"},
                }
            )
            raise subprocess.TimeoutExpired(
                self.command,
                timeout,
                output=partial + "\n",
                stderr="partial stderr",
            )
        return "", ""

    def poll(self) -> int | None:
        return self.returncode


def _settings(**overrides: object) -> AgentRuntimeSettings:
    values: dict[str, object] = {
        "adapter": "codex-cli",
        "executable": "codex",
        "provider_managed_auth": True,
        "model": "gpt-5.5",
        "timeout_seconds": 15,
    }
    values.update(overrides)
    return AgentRuntimeSettings(**values)


def test_fresh_turn_uses_real_cwd_sandbox_images_ephemeral_and_minimal_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SuccessfulPopen.instances.clear()
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        "alysis_code.agent_runtimes.codex_cli._resolve_executable",
        lambda _configured: "/opt/codex/bin/codex",
    )
    monkeypatch.setattr(subprocess, "Popen", _SuccessfulPopen)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("CODEX_HOME", "/home/tester/.codex")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("DATABASE_URL", "must-not-leak")

    result = CodexCliRuntimeAdapter().run_turn(
        _settings(),
        RuntimeTurnRequest(
            prompt="Inspect the repository",
            cwd=tmp_path,
            mode="readonly",
            images=(Path("diagram.png"),),
            no_log=True,
        ),
    )

    process = _SuccessfulPopen.instances[-1]
    command = process.command
    assert command[:3] == ["/opt/codex/bin/codex", "exec", "--json"]
    assert "resume" not in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("-C") + 1] == os.fspath(tmp_path.resolve())
    assert 'forced_login_method="chatgpt"' in command
    assert command[command.index("--model") + 1] == "gpt-5.5"
    assert command[command.index("--image") + 1] == os.fspath(image.resolve())
    assert "--skip-git-repo-check" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--ephemeral" in command
    assert command[-1] == "-"
    assert process.kwargs["cwd"] == os.fspath(tmp_path.resolve())
    runtime_env = process.kwargs["env"]
    assert runtime_env["CODEX_HOME"] == "/home/tester/.codex"
    assert runtime_env["HOME"] == "/home/tester"
    assert runtime_env["PATH"] == "/usr/bin"
    assert "OPENAI_API_KEY" not in runtime_env
    assert "ANTHROPIC_API_KEY" not in runtime_env
    assert "DATABASE_URL" not in runtime_env
    if os.name == "nt":
        assert "creationflags" in process.kwargs
    else:
        assert process.kwargs["start_new_session"] is True
    assert process.input == "Inspect the repository"
    assert process.timeout == 15
    assert result.ok is True
    assert result.final_message == "captured final message"
    assert result.session_id is None
    assert result.usage == {"input_tokens": 40, "output_tokens": 8}
    assert len(result.events) == 3
    assert result.warnings == ("Ignored invalid Codex JSONL event on line 4.",)


def test_resume_turn_uses_resume_grammar_and_workspace_write_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SuccessfulPopen.instances.clear()
    monkeypatch.setattr(
        "alysis_code.agent_runtimes.codex_cli._resolve_executable",
        lambda _configured: "/opt/codex/bin/codex",
    )
    monkeypatch.setattr(subprocess, "Popen", _SuccessfulPopen)

    result = CodexCliRuntimeAdapter().run_turn(
        _settings(model=None),
        RuntimeTurnRequest(
            prompt="Continue the work",
            cwd=tmp_path,
            mode="auto",
            session_id=_OTHER_THREAD_ID,
        ),
    )

    command = _SuccessfulPopen.instances[-1].command
    assert command[:4] == ["/opt/codex/bin/codex", "exec", "resume", "--json"]
    assert 'sandbox_mode="workspace-write"' in command
    assert "--sandbox" not in command
    assert "-C" not in command
    assert "--model" not in command
    assert "--ephemeral" not in command
    assert command[-3:] == ["--", _OTHER_THREAD_ID, "-"]
    assert result.session_id == _THREAD_ID


def test_resume_turn_rejects_option_shaped_or_non_uuid_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Codex must not start for an invalid session id")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with pytest.raises(ValueError, match="must be UUIDs"):
        CodexCliRuntimeAdapter().run_turn(
            _settings(),
            RuntimeTurnRequest(
                prompt="continue",
                cwd=tmp_path,
                session_id="--dangerously-bypass-approvals-and-sandbox",
            ),
        )


@pytest.mark.parametrize("mode", ["fullaccess"])
def test_turn_explicitly_rejects_fullaccess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    def fail_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Codex must not start for fullaccess")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with pytest.raises(ValueError, match="does not support fullaccess"):
        CodexCliRuntimeAdapter().run_turn(
            _settings(),
            RuntimeTurnRequest(prompt="unsafe", cwd=tmp_path, mode=mode),  # type: ignore[arg-type]
        )


def test_turn_timeout_cleans_process_tree_and_returns_partial_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _TimeoutPopen.instances.clear()
    terminated: list[_TimeoutPopen] = []
    monkeypatch.setattr(
        "alysis_code.agent_runtimes.codex_cli._resolve_executable",
        lambda _configured: "/opt/codex/bin/codex",
    )
    monkeypatch.setattr(subprocess, "Popen", _TimeoutPopen)

    def fake_terminate(process: _TimeoutPopen) -> None:
        terminated.append(process)
        process.returncode = -getattr(signal, "SIGKILL", signal.SIGTERM)

    monkeypatch.setattr(
        "alysis_code.agent_runtimes.codex_cli._terminate_process_tree",
        fake_terminate,
    )

    result = CodexCliRuntimeAdapter().run_turn(
        _settings(timeout_seconds=0.25),
        RuntimeTurnRequest(prompt="slow task", cwd=tmp_path, mode="review"),
    )

    assert terminated == [_TimeoutPopen.instances[-1]]
    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.error == "Codex turn timed out after 0.25 seconds."
    assert result.final_message == "partial final"
    assert len(result.events) == 1


def test_keyboard_interrupt_cleans_process_tree_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[object] = []

    class InterruptPopen:
        pid = 9753
        returncode: int | None = None

        def __init__(self, _command: list[str], **_kwargs: Any) -> None:
            pass

        def communicate(
            self,
            input: str | None = None,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            del input, timeout
            raise KeyboardInterrupt

        def poll(self) -> int | None:
            return self.returncode

    monkeypatch.setattr(
        "alysis_code.agent_runtimes.codex_cli._resolve_executable",
        lambda _configured: "/opt/codex/bin/codex",
    )
    monkeypatch.setattr(subprocess, "Popen", InterruptPopen)
    monkeypatch.setattr(
        "alysis_code.agent_runtimes.codex_cli._terminate_process_tree",
        lambda process: terminated.append(process),
    )

    with pytest.raises(KeyboardInterrupt):
        CodexCliRuntimeAdapter().run_turn(
            _settings(),
            RuntimeTurnRequest(prompt="interrupt me", cwd=tmp_path),
        )

    assert len(terminated) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_process_tree_cleanup_signals_dedicated_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []

    class FakeProcess:
        pid = 2468
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            self.returncode = -signal.SIGTERM
            return self.returncode

        def terminate(self) -> None:
            raise AssertionError("dedicated process group should be used")

        def kill(self) -> None:
            raise AssertionError("dedicated process group should be used")

    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    _terminate_process_tree(FakeProcess())  # type: ignore[arg-type]

    assert signals == [
        (2468, signal.SIGTERM),
        (2468, signal.SIGKILL),
    ]


def test_runtime_environment_never_forwards_provider_credentials() -> None:
    result = _runtime_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "CODEX_HOME": "/home/tester/.codex",
            "BROWSER": "xdg-open",
            "DISPLAY": ":0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-0",
            "OPENAI_API_KEY": "secret",
            "XAI_API_KEY": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "/secret.json",
            "UNRELATED": "discard",
        }
    )

    assert result == {
        "PATH": "/usr/bin",
        "HOME": "/home/tester",
        "CODEX_HOME": "/home/tester/.codex",
        "BROWSER": "xdg-open",
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "WAYLAND_DISPLAY": "wayland-0",
    }
