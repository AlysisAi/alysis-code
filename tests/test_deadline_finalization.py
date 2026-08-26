from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.session_store import read_session_events


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CapturingClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = tools, stream, on_text_delta, temperature
        self.calls.append({"messages": list(messages)})
        return LLMResponse(content="Final answer.", tool_calls=[], raw={})


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def test_finalization_window_adds_single_materialization_directive(tmp_path: Path) -> None:
    clock = _FakeClock(10.0)
    deadline = ExecutionDeadline.from_duration(4.0, clock=clock, source="explicit_cli")
    clock.advance(3.2)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        enable_compaction=False,
        verification_enabled=False,
    )
    client = _CapturingClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Summarize this repository state.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 1
    system_messages = [
        str(message.get("content") or "")
        for message in client.calls[0]["messages"]
        if message.get("role") == "system"
    ]
    finalization_messages = [
        message
        for message in system_messages
        if "Run deadline finalization window is active." in message
    ]
    assert len(finalization_messages) == 1
    assert "Materialize the best valid result now." in finalization_messages[0]
    assert _event_payloads(log_path, "deadline_finalization_started")
    directive_events = _event_payloads(log_path, "deadline_finalization_directive")
    assert len(directive_events) == 1
    assert directive_events[0]["deadline"]["phase"] == "finalization_window"


def test_one_shot_without_deadline_emits_unconfigured_deadline_event(tmp_path: Path) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        one_shot_execution=True,
        enable_compaction=False,
        verification_enabled=False,
    )
    client = _CapturingClient()
    session.client = client  # type: ignore[assignment]

    try:
        session.run_turn("Summarize this repository state.")
        log_path = session.store.path
    finally:
        session.close()

    events = _event_payloads(log_path, "run_deadline_unconfigured")
    assert len(events) == 1
    assert events[0]["deadline_config_source"] == "absent"
    assert events[0]["runtime_kind"] == "one_shot"
