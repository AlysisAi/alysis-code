from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code import agent_loop as agent_loop_mod
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


class _LoopingToolClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0
        self.tool_enabled_calls = 0
        self.call_tools: list[list[dict[str, Any]] | None] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        self.calls += 1
        self.call_tools.append(tools)
        if tools is None:
            return LLMResponse(
                content=(
                    "Completed work: partial repo inspection.\n"
                    "Remaining work: implementation is unfinished.\n"
                    "Known issues or risks: step budget exhausted."
                ),
                tool_calls=[],
                raw={},
            )
        self.tool_enabled_calls += 1
        return LLMResponse(
            content="Still working.",
            tool_calls=[
                ToolCall(
                    id=f"call-{self.tool_enabled_calls}",
                    name="fs_list",
                    arguments={"path": "."},
                )
            ],
            raw={},
        )


class _FinalReplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, reply: str) -> None:
        self.reply = reply
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
        self.calls += 1
        return LLMResponse(content=self.reply, tool_calls=[], raw={})


class _CompletesAfterToolCallsClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, *, complete_after: int) -> None:
        self.complete_after = complete_after
        self.calls = 0
        self.tool_enabled_calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        self.calls += 1
        if tools is None:
            return LLMResponse(content="Inspection complete.", tool_calls=[], raw={})
        if self.tool_enabled_calls >= self.complete_after:
            return LLMResponse(content="Inspection complete.", tool_calls=[], raw={})
        self.tool_enabled_calls += 1
        return LLMResponse(
            content="Continuing the inspection.",
            tool_calls=[
                ToolCall(
                    id=f"call-{self.tool_enabled_calls}",
                    name="fs_list",
                    arguments={"path": "."},
                )
            ],
            raw={},
        )


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def test_autonomous_repo_turn_runs_past_legacy_cap_until_model_completes(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(model="test-model", routing_mode="auto", step_budget_policy="adaptive")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=5,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
    )
    client = _CompletesAfterToolCallsClient(complete_after=8)
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Inspect this repository and report your findings.")
        active_turn_budget = session.step_budget_runtime.active_turn_budget
        last_resolution = session.step_budget_runtime.last_resolution
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert last_resolution is not None
    assert last_resolution.resolved_max_steps is None
    assert last_resolution.unlimited is True
    assert client.tool_enabled_calls == 8
    assert client.calls == 9
    assert client.tool_enabled_calls > session.max_steps
    assert active_turn_budget is None
    assert last_resolution.reason == "autonomous_unbounded"
    resolution_events = _event_payloads(log_path, "turn_step_budget_resolved")
    assert len(resolution_events) == 1
    assert resolution_events[0]["resolved_max_steps"] is None
    assert resolution_events[0]["unlimited"] is True
    assert _event_payloads(log_path, "error") == []
    assert _event_payloads(log_path, "interactive_step_budget_handoff") == []
    assert _event_payloads(log_path, "forced_final_summary_requested") == []


def test_simple_agent_fixed_override_uses_fixed_turn_ceiling(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", routing_mode="auto", step_budget_policy="adaptive")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
        chat_turn_fixed_override=4,
    )
    client = _LoopingToolClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Keep working on the repo change.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.tool_enabled_calls == 4
    assert client.calls == 5
    assert client.call_tools[-1] is None
    resolution_events = _event_payloads(log_path, "turn_step_budget_resolved")
    assert len(resolution_events) == 1
    assert resolution_events[0]["resolved_max_steps"] == 4
    assert resolution_events[0]["reason"] == "explicit_limit"
    assert resolution_events[0]["override_applied"] is True
    assert _event_payloads(log_path, "error") == []
    assert _event_payloads(log_path, "interactive_step_budget_handoff")


def test_low_level_session_can_still_use_an_explicit_finite_loop(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(model="test-model", routing_mode="auto", step_budget_policy="adaptive")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
    )
    client = _LoopingToolClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Keep working on the repo change.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert client.tool_enabled_calls == 12
    assert client.calls == 13
    assert client.call_tools[-1] is None
    assert session.step_budget_runtime.active_turn_budget is None
    assert _event_payloads(log_path, "turn_step_budget_resolved") == []
    error_events = _event_payloads(log_path, "error")
    assert error_events[-1]["max_steps"] == 12


def test_deadline_exhausted_before_first_llm_skips_model_and_not_step_budget(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=10.0,
        deadline_monotonic=10.0,
        configured_duration_seconds=1.0,
        clock=lambda: 10.0,
    )
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        enable_compaction=False,
    )
    client = _LoopingToolClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Keep working on the repo change.")
        log_path = session.store.path
    finally:
        session.close()

    # A budget stop is a normal outcome and exits clean, even though the model
    # was never reached: nothing went wrong, there was simply no time left.
    assert exit_code == 0
    assert client.calls == 0
    deadline_events = _event_payloads(log_path, "deadline_exhausted")
    assert deadline_events[-1]["operation"] == "step_loop"
    assert deadline_events[-1]["stop_reason"] == "run_budget_exhausted"
    assert deadline_events[-1]["deadline_exhausted"] is True
    forced_summary_requests = _event_payloads(log_path, "forced_final_summary_requested")
    assert forced_summary_requests[-1]["termination_kind"] == "deadline_exhausted"
    assert forced_summary_requests[-1]["termination_cause"] == "the run deadline is exhausted"
    error_events = _event_payloads(log_path, "error")
    assert all(event.get("max_steps") is None for event in error_events)


def test_chat_only_turn_adds_no_step_budget_resolution(
    tmp_path: Path,
) -> None:
    # Router-free path: there is no non-repo fast path. The bounded /chat turn
    # is the one surface that answers without entering the repo loop, and it
    # must not resolve (or leak) a per-turn step budget.
    cfg = AppConfig(model="test-model", routing_mode="auto", step_budget_policy="adaptive")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=64,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
    )
    client = _FinalReplyClient("Blocked by missing requirements.")
    session.client = client  # type: ignore[assignment]

    try:
        first_exit_code = session.run_turn("Handle this repo task.")
        assert session.step_budget_runtime.last_resolution is not None
        assert (
            session.step_budget_runtime.active_turn_budget
            == session.step_budget_runtime.last_resolution.resolved_max_steps
        )
        assert session.step_budget_runtime.active_turn_budget is None
        second_exit_code = session.run_turn("How are you?", chat_only=True)
        active_turn_budget = session.step_budget_runtime.active_turn_budget
        log_path = session.store.path
    finally:
        session.close()

    assert first_exit_code == 0
    assert second_exit_code == 0
    assert client.calls == 2
    assert active_turn_budget is None
    assert len(_event_payloads(log_path, "turn_step_budget_resolved")) == 1


def test_run_agent_defaults_chat_turn_budget_off(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _DummySession:
        def run_turn(
            self,
            instruction: str,
            *,
            image_paths: list[str] | None = None,
            cancellation_token: Any | None = None,
        ) -> int:
            captured["instruction"] = instruction
            captured["image_paths"] = image_paths
            captured["cancellation_token"] = cancellation_token
            return 0

        def close(self) -> None:
            captured["closed"] = True

    def fake_create_session(**kwargs: Any) -> _DummySession:
        captured.update(kwargs)
        return _DummySession()

    monkeypatch.setattr(agent_loop_mod, "create_session", fake_create_session)

    exit_code = agent_loop_mod.run_agent(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        instruction="hi",
        mode="review",
        yes=True,
        max_steps=7,
        no_log=True,
        api_key_override="override-key",
    )

    assert exit_code == 0
    assert captured["enable_chat_turn_step_budget"] is False
    assert captured["chat_turn_fixed_override"] is None
    assert captured["closed"] is True
