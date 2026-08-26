from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code.agent_loop import (
    _FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT,
    _LOW_STEP_BUDGET_SYSTEM_PROMPT_TEMPLATE,
    _PHASE_BUDGET_EXPLORATION_SYSTEM_PROMPT_TEMPLATE,
    _request_messages_with_ephemeral_system_prompt_suffixes,
    _request_messages_with_ephemeral_system_prompts,
    create_session,
)
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events

SMOKE_MODEL = "gpt-4o-mini"


class _ScriptedClient:
    model = SMOKE_MODEL
    temperature = 0.2

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        finalization_response: LLMResponse | None = None,
    ) -> None:
        self._responses = responses
        self._finalization_response = finalization_response
        self.calls: list[dict[str, Any]] = []
        self._tool_enabled_calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = on_text_delta, temperature
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "stream": stream,
            }
        )
        if tools is None:
            assert self._finalization_response is not None
            return self._finalization_response
        response = self._responses[self._tool_enabled_calls]
        self._tool_enabled_calls += 1
        return response


class _UnexpectedChatClient:
    model = SMOKE_MODEL
    temperature = 0.2

    def __init__(self) -> None:
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
        raise AssertionError("non-repo fast path should not call the main client")


def _tool_call(step: int) -> ToolCall:
    return ToolCall(
        id=f"call-{step}",
        name="fs_list",
        arguments={"path": "."},
    )


def _has_final_step_nudge(messages: list[dict[str, Any]]) -> bool:
    return any(
        str(message.get("role")) == "system"
        and str(message.get("content") or "") == _FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT
        for message in messages
    )


def _has_low_step_budget_nudge(messages: list[dict[str, Any]], *, remaining: int) -> bool:
    expected = _LOW_STEP_BUDGET_SYSTEM_PROMPT_TEMPLATE.format(remaining_steps=remaining)
    return any(
        str(message.get("role")) == "system" and str(message.get("content") or "") == expected
        for message in messages
    )


def _has_phase_exploration_nudge(messages: list[dict[str, Any]], *, exploration_steps: int) -> bool:
    expected = _PHASE_BUDGET_EXPLORATION_SYSTEM_PROMPT_TEMPLATE.format(
        exploration_steps=exploration_steps
    )
    return any(
        str(message.get("role")) == "system" and str(message.get("content") or "") == expected
        for message in messages
    )


def _final_step_nudge_indexes(messages: list[dict[str, Any]]) -> list[int]:
    return [
        idx
        for idx, message in enumerate(messages)
        if str(message.get("role")) == "system"
        and str(message.get("content") or "") == _FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT
    ]


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def test_final_step_nudge_only_applies_on_last_productive_call(tmp_path: Path) -> None:
    session = create_session(
        cfg=AppConfig(
            model=SMOKE_MODEL,
            routing_mode="code_only",
            step_budget_policy="limited",
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    client = _ScriptedClient(
        [
            LLMResponse(content="Inspecting.", tool_calls=[_tool_call(1)], raw={}),
            LLMResponse(content="Still checking.", tool_calls=[_tool_call(2)], raw={}),
            LLMResponse(content="Blocked by missing requirements.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Finish the repo task cleanly.")
        persisted_messages = list(session.messages)
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 3
    assert all(call["tools"] is not None for call in client.calls)
    assert not _has_final_step_nudge(client.calls[0]["messages"])
    assert not _has_final_step_nudge(client.calls[1]["messages"])
    assert _has_final_step_nudge(client.calls[2]["messages"])
    assert _final_step_nudge_indexes(client.calls[2]["messages"]) == [
        len(client.calls[2]["messages"]) - 1
    ]
    assert not any(
        str(message.get("content") or "") == _FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT
        for message in persisted_messages
    )


def test_low_step_budget_nudge_applies_before_final_step(tmp_path: Path) -> None:
    session = create_session(
        cfg=AppConfig(
            model=SMOKE_MODEL,
            routing_mode="code_only",
            step_budget_policy="limited",
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    client = _ScriptedClient(
        [
            LLMResponse(content="Inspecting.", tool_calls=[_tool_call(1)], raw={}),
            LLMResponse(content="Checking.", tool_calls=[_tool_call(2)], raw={}),
            LLMResponse(content="Integrating.", tool_calls=[_tool_call(3)], raw={}),
            LLMResponse(content="Blocked by missing requirements.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Finish the repo task cleanly.")
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 4
    assert _has_low_step_budget_nudge(client.calls[0]["messages"], remaining=3)
    assert _has_low_step_budget_nudge(client.calls[1]["messages"], remaining=2)
    assert _has_low_step_budget_nudge(client.calls[2]["messages"], remaining=1)
    assert not _has_low_step_budget_nudge(client.calls[3]["messages"], remaining=0)
    assert _has_final_step_nudge(client.calls[3]["messages"])


def test_interactive_phase_budget_nudges_after_repeated_exploration(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    client = _ScriptedClient(
        [
            LLMResponse(content="Inspecting.", tool_calls=[_tool_call(1)], raw={}),
            LLMResponse(content="Checking.", tool_calls=[_tool_call(2)], raw={}),
            LLMResponse(content="Still mapping.", tool_calls=[_tool_call(3)], raw={}),
            LLMResponse(content="Blocked by missing requirements.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement the repo change.")
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 4
    assert not _has_phase_exploration_nudge(client.calls[0]["messages"], exploration_steps=3)
    assert _has_phase_exploration_nudge(client.calls[3]["messages"], exploration_steps=3)


def test_phase_budget_exploration_prompt_is_capped_but_telemetry_continues(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=20,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    responses = [
        LLMResponse(content=f"Inspecting {idx}.", tool_calls=[_tool_call(idx)], raw={})
        for idx in range(1, 9)
    ]
    responses.append(LLMResponse(content="Blocked by missing requirements.", tool_calls=[], raw={}))
    client = _ScriptedClient(responses)
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Investigate the repo before changing it.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    injected_steps = [
        step
        for step, call in enumerate(client.calls, start=1)
        if any(
            str(message.get("role")) == "system"
            and str(message.get("content") or "").startswith("Phase budget pressure:")
            for message in call["messages"]
        )
    ]
    assert injected_steps == [4, 7]
    assert _has_phase_exploration_nudge(client.calls[3]["messages"], exploration_steps=3)
    assert _has_phase_exploration_nudge(client.calls[6]["messages"], exploration_steps=6)

    intervention_events = [
        payload
        for payload in _event_payloads(log_path, "controller_intervention")
        if payload.get("detail") == "phase_budget_exploration_prompt"
    ]
    assert [event["step"] for event in intervention_events] == [4, 5, 6, 7, 8, 9]
    assert [event["headline_counted"] for event in intervention_events] == [
        True,
        False,
        False,
        True,
        False,
        False,
    ]
    assert [event["metadata"]["exploration_steps"] for event in intervention_events] == [
        3,
        4,
        5,
        6,
        7,
        8,
    ]
    suppressed_events = [event for event in intervention_events if not event["headline_counted"]]
    assert suppressed_events
    assert all(event["metadata"]["suppressed"] is True for event in suppressed_events)
    headline_totals = [event["controller_interventions_total"] for event in intervention_events]
    assert headline_totals[1] == headline_totals[0]
    assert headline_totals[2] == headline_totals[0]
    assert headline_totals[3] == headline_totals[0] + 1
    assert headline_totals[4] == headline_totals[3]
    assert headline_totals[5] == headline_totals[3]


def test_final_step_nudge_keeps_tools_available_before_forced_summary(tmp_path: Path) -> None:
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
    )
    client = _ScriptedClient(
        [
            LLMResponse(content="Inspecting.", tool_calls=[_tool_call(1)], raw={}),
            LLMResponse(content="Still working.", tool_calls=[_tool_call(2)], raw={}),
        ],
        finalization_response=LLMResponse(
            content=(
                "Completed work: partial repo inspection.\n"
                "Remaining work: implementation is unfinished.\n"
                "Known issues or risks: step budget exhausted."
            ),
            tool_calls=[],
            raw={},
        ),
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Keep working on the repo task until it is done.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert len(client.calls) == 3
    assert client.calls[0]["tools"] is not None
    assert client.calls[1]["tools"] is not None
    assert client.calls[2]["tools"] is None
    assert not _has_final_step_nudge(client.calls[0]["messages"])
    assert _has_final_step_nudge(client.calls[1]["messages"])
    assert _final_step_nudge_indexes(client.calls[1]["messages"]) == [
        len(client.calls[1]["messages"]) - 1
    ]
    assert _event_payloads(log_path, "forced_final_summary_requested")
    assert _event_payloads(log_path, "forced_final_summary_completed")


def test_final_step_nudge_preserves_stable_request_prefix_and_only_adds_suffix() -> None:
    base_messages = [
        {"role": "system", "content": "base system"},
        {"role": "user", "content": "Do the repo task."},
        {"role": "assistant", "content": "Inspecting."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "fs_list", "arguments": '{"path": "."}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"entries": []}',
        },
    ]
    turn_wrapper = "Plan Mode wrapper prompt"

    non_final_request = _request_messages_with_ephemeral_system_prompts(
        messages=base_messages,
        insert_index=2,
        prompts=[turn_wrapper],
    )
    final_request = _request_messages_with_ephemeral_system_prompt_suffixes(
        messages=non_final_request,
        prompts=[_FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT],
    )

    assert non_final_request == final_request[:-1]
    assert final_request[-1] == {
        "role": "system",
        "content": _FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT,
    }
    assert final_request[2] == {"role": "system", "content": turn_wrapper}


def test_chat_only_turn_never_requests_forced_final_summary(tmp_path: Path) -> None:
    # Router-free path: the bounded /chat turn answers in one model call and
    # never trips the step-budget or forced-final-summary machinery.
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="auto"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=True,
    )
    # /chat turns request no tool schemas, which this file's scripted client
    # models as its tool-free "finalization" call shape.
    client = _ScriptedClient(
        [],
        finalization_response=LLMResponse(content="I am doing well.", tool_calls=[], raw={}),
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("How are you?", chat_only=True)
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 1
    assert _event_payloads(log_path, "forced_final_summary_requested") == []
