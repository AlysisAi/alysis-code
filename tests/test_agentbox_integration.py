from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agentbox_integration as agentbox_integration
from alysis_code.agent_loop import create_session
from alysis_code.agentbox_integration import (
    AgentBoxTelemetry,
    sanitize_task_hint,
    tool_category,
)
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall


class _ScriptedClient:
    def __init__(self, responses: list[LLMResponse], *, model: str = "test-model") -> None:
        self.model = model
        self.temperature = 0.2
        self._responses = responses
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _FakeAgentBox:
    instances: list[_FakeAgentBox] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.events: list[tuple[Any, ...]] = []
        self.__class__.instances.append(self)

    def session(self, workspace: str | None = None) -> _FakeSessionContext:
        return _FakeSessionContext(self, workspace)

    def close(self) -> None:
        self.events.append(("client.close",))


class _FakeSessionContext:
    def __init__(self, client: _FakeAgentBox, workspace: str | None) -> None:
        self.client = client
        self.workspace = workspace

    def __enter__(self) -> _FakeSession:
        self.client.events.append(("session.start", self.workspace))
        return _FakeSession(self.client)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc, tb
        self.client.events.append(("session.end", exc_type is not None))


class _FakeSession:
    def __init__(self, client: _FakeAgentBox) -> None:
        self.client = client

    def task(self, hint: str) -> None:
        self.client.events.append(("task.update", hint))

    def turn(self) -> _FakeTurnContext:
        return _FakeTurnContext(self.client)

    def tokens(self, in_: int = 0, out: int = 0, usd: float | None = None) -> None:
        self.client.events.append(("tokens", in_, out, usd))

    def tool(self, name: str, category: str = "other", count: int = 1) -> None:
        self.client.events.append(("tool.activity", name, category, count))


class _FakeTurnContext:
    def __init__(self, client: _FakeAgentBox) -> None:
        self.client = client

    def __enter__(self) -> None:
        self.client.events.append(("turn.start",))
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc, tb
        self.client.events.append(("turn.end", exc_type is not None))


def _install_fake_agentbox(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAgentBox]:
    _FakeAgentBox.instances.clear()
    monkeypatch.setattr(agentbox_integration, "AgentBoxClient", _FakeAgentBox)
    return _FakeAgentBox


def _enable_agentbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTBOX_ENABLED", "1")
    monkeypatch.setenv("AGENTBOX_PLANE_URL", "http://agentbox.local")
    monkeypatch.setenv("AGENTBOX_TOKEN", "test-token")
    monkeypatch.setenv("AGENTBOX_QUEUE_DIR", str(tmp_path / "queue"))


def _clear_agentbox_connection_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AGENTBOX_PLANE_URL",
        "AGENTBOX_TOKEN",
        "AGENTBOX_ORG_ID",
        "AGENTBOX_PERSON_ID",
        "AGENTBOX_MACHINE_ID",
        "AGENTBOX_AGENT_ID",
        "AGENTBOX_QUEUE_DIR",
        "AGENTBOX_TASK_DETAIL",
        "AGENTBOX_CONFIG",
        "AGENTBOX_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


def _session_for(
    root: Path,
    *,
    max_steps: int = 4,
    no_log: bool = True,
) -> Any:
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=root,
        mode="auto",
        yes=True,
        max_steps=max_steps,
        no_log=no_log,
        api_key_override="override-key",
    )


def test_agentbox_disabled_mode_does_not_create_or_emit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentbox = _install_fake_agentbox(monkeypatch)
    monkeypatch.setenv("AGENTBOX_ENABLED", "0")
    monkeypatch.setenv("AGENTBOX_PLANE_URL", "http://agentbox.local")
    monkeypatch.setenv("AGENTBOX_TOKEN", "test-token")

    assert AgentBoxTelemetry.from_env(root=tmp_path, runtime_version="test") is None
    assert fake_agentbox.instances == []

    session = _session_for(tmp_path, max_steps=2)
    session.client = _ScriptedClient([LLMResponse(content="done", tool_calls=[], raw={})])

    try:
        assert session.run_turn("Do a tiny task.") == 0
    finally:
        session.close()

    assert session.agentbox_telemetry is None
    assert fake_agentbox.instances == []


def test_agentbox_run_turn_emits_metadata_only_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentbox = _install_fake_agentbox(monkeypatch)
    _enable_agentbox(monkeypatch, tmp_path)
    root = tmp_path / "repo-name"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")

    session = _session_for(root)
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="Reading.",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "README.md"})],
                raw={},
                usage=LLMUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            ),
            LLMResponse(
                content="Done.",
                tool_calls=[],
                raw={},
                usage=LLMUsage(prompt_tokens=8, completion_tokens=3, total_tokens=11),
            ),
            LLMResponse(
                content="Verified.",
                tool_calls=[],
                raw={},
                usage=LLMUsage(prompt_tokens=4, completion_tokens=1, total_tokens=5),
            ),
        ]
    )

    try:
        assert (
            session.run_turn(
                "Inspect /Users/bings_jr/alysis internal/src/auth.py and summarize `def x()`."
            )
            == 0
        )
        assert session.run_turn("Run the verification step.") == 0
    finally:
        session.close()

    agentbox = fake_agentbox.instances[0]
    assert agentbox.events.count(("session.start", "repo-name")) == 1
    assert agentbox.events.count(("session.end", False)) == 1
    assert agentbox.events.count(("turn.start",)) == 2
    assert agentbox.events.count(("turn.end", False)) == 2
    assert ("tool.activity", "fs_read", "read", 1) in agentbox.events
    assert ("tokens", 5, 2, None) in agentbox.events
    assert ("tokens", 13, 5, None) in agentbox.events
    assert ("tokens", 4, 1, None) in agentbox.events

    task_events = [event[1] for event in agentbox.events if event[0] == "task.update"]
    assert task_events == [
        "Inspect file and summarize.",
        "Run the verification step.",
    ]
    assert not any("/Users" in event or "def x" in event for event in task_events)


def test_agentbox_uses_enrolled_machine_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentbox = _install_fake_agentbox(monkeypatch)
    _clear_agentbox_connection_env(monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'plane_url = "https://agentbox.example.test"',
                'machine_token = "machine-token"',
                'org_id = "org_a"',
                'person_id = "person_a"',
                'machine_id = "machine_b"',
                'task_detail = "category"',
                # TOML basic strings treat backslashes as escapes. Use a
                # portable path spelling so the enrolled-config fixture stays
                # valid on Windows as well as POSIX hosts.
                f'queue_dir = "{(tmp_path / "queue").as_posix()}"',
                "",
                "[runtime.alysis]",
                'agent_id = "machine_b_alysis_custom"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTBOX_ENABLED", "1")
    monkeypatch.setenv("AGENTBOX_CONFIG", str(config_path))

    telemetry = AgentBoxTelemetry.from_env(root=tmp_path, runtime_version="test")

    assert telemetry is not None
    agentbox = fake_agentbox.instances[0]
    assert agentbox.kwargs == {
        "token": "machine-token",
        "plane_url": "https://agentbox.example.test",
        "org_id": "org_a",
        "person_id": "person_a",
        "machine_id": "machine_b",
        "agent_id": "machine_b_alysis_custom",
        "runtime_version": "test",
        "queue_dir": str(tmp_path / "sdk-queue"),
        "task_detail": "category",
    }
    telemetry.close()


def test_agentbox_environment_overrides_enrolled_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentbox = _install_fake_agentbox(monkeypatch)
    _clear_agentbox_connection_env(monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                'plane_url = "https://config.example.test"',
                'machine_token = "config-token"',
                'machine_id = "config-machine"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTBOX_ENABLED", "1")
    monkeypatch.setenv("AGENTBOX_CONFIG", str(config_path))
    monkeypatch.setenv("AGENTBOX_PLANE_URL", "https://env.example.test")
    monkeypatch.setenv("AGENTBOX_TOKEN", "env-token")
    monkeypatch.setenv("AGENTBOX_MACHINE_ID", "env-machine")
    monkeypatch.setenv("AGENTBOX_AGENT_ID", "env-agent")

    telemetry = AgentBoxTelemetry.from_env(root=tmp_path, runtime_version="test")

    assert telemetry is not None
    assert fake_agentbox.instances[0].kwargs["plane_url"] == "https://env.example.test"
    assert fake_agentbox.instances[0].kwargs["token"] == "env-token"
    assert fake_agentbox.instances[0].kwargs["machine_id"] == "env-machine"
    assert fake_agentbox.instances[0].kwargs["agent_id"] == "env-agent"
    telemetry.close()


def test_agentbox_ignores_invalid_or_incomplete_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentbox = _install_fake_agentbox(monkeypatch)
    _clear_agentbox_connection_env(monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text('machine_token = "unterminated', encoding="utf-8")
    monkeypatch.setenv("AGENTBOX_ENABLED", "1")
    monkeypatch.setenv("AGENTBOX_CONFIG", str(config_path))

    assert AgentBoxTelemetry.from_env(root=tmp_path, runtime_version="test") is None
    assert fake_agentbox.instances == []


def test_agentbox_task_sanitizer_removes_content_and_paths() -> None:
    hint = sanitize_task_hint(
        "Refactor /Users/bings_jr/project/src/auth.py then ```secret diff``` and `def login()` "
        + ("x" * 200)
    )

    assert 0 < len(hint) <= 140
    assert "/Users" not in hint
    assert "secret diff" not in hint
    assert "def login" not in hint


def test_agentbox_tool_category_mapping_matches_event_contract() -> None:
    assert tool_category("fs_read") == "read"
    assert tool_category("fs_write") == "edit"
    assert tool_category("shell_run") == "exec"
    assert tool_category("web_fetch") == "net"
    assert tool_category("custom_agentbox_probe") == "other"


def test_unreachable_agentbox_plane_does_not_fail_alysis_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTBOX_ENABLED", "1")
    monkeypatch.setenv("AGENTBOX_PLANE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENTBOX_TOKEN", "test-token")
    monkeypatch.setenv("AGENTBOX_MACHINE_ID", "test-machine")
    monkeypatch.delenv("AGENTBOX_CONFIG", raising=False)
    monkeypatch.setenv("AGENTBOX_HOME", str(tmp_path / "agentbox-home"))
    monkeypatch.setenv("AGENTBOX_QUEUE_DIR", str(tmp_path / "agentbox-queue"))

    session = _session_for(tmp_path, max_steps=2)
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="done",
                tool_calls=[],
                raw={},
                usage=LLMUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )
        ]
    )

    started = time.monotonic()
    try:
        assert session.run_turn("Complete a short no-op task.") == 0
    finally:
        session.close()

    assert time.monotonic() - started < 5
