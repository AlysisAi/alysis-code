from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.cancellation import CooperativeCancellationError
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import DeadlineExhausted
from alysis_code.hooks.models import HookDispatchResult
from alysis_code.llm.types import LLMError, LLMResponse, ToolCall


class _ScriptedClient:
    model = "test-model"
    temperature = 0.0
    supports_tool_calling = True

    def __init__(self, responses: Sequence[LLMResponse | BaseException]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,
        on_reasoning_delta=None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancellation_token=None,
        tool_choice=None,
    ) -> LLMResponse:
        del on_text_delta, on_reasoning_delta, cancellation_token, tool_choice
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "stream": stream,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _MutateFirstSkillReadHook:
    def __init__(self, modified_input: dict[str, Any]) -> None:
        self._modified_input = modified_input
        self._mutated = False

    def fire_user_prompt_submit(self, **payload: Any) -> HookDispatchResult:
        del payload
        return HookDispatchResult()

    def fire_pre_tool_use(self, **payload: Any) -> HookDispatchResult:
        if payload.get("tool_name") == "skill_read" and not self._mutated:
            self._mutated = True
            return HookDispatchResult(modified_input=dict(self._modified_input))
        return HookDispatchResult()

    def fire_post_tool_use(self, **payload: Any) -> HookDispatchResult:
        del payload
        return HookDispatchResult()

    def fire_turn_complete(self, **payload: Any) -> HookDispatchResult:
        del payload
        return HookDispatchResult()

    def fire_session_end(self, **payload: Any) -> HookDispatchResult:
        del payload
        return HookDispatchResult()


def _write_skill(root: Path, name: str, description: str, body: str) -> None:
    bundle = root / ".alysis_skills" / name
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _skill_call(call_id: str, name: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name="skill_read", arguments={"name": name})],
        raw={},
    )


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        raw={},
    )


def _session(tmp_path: Path, *, max_steps: int = 4):
    return create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            bundled_skills_enabled=False,
            skills_enabled=True,
            skills_auto_invoke=True,
        ),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=max_steps,
        no_log=True,
        api_key_override="override-key",
    )


def _event_payloads(session: Any, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in session.store.events_snapshot()
        if event.get("type") == event_type
    ]


def test_semantic_selection_blocks_other_actions_until_selected_skill_is_read(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "broad-workflow",
        "Handle a broad class of repository maintenance requests.",
        "BROAD WORKFLOW BODY",
    )
    _write_skill(
        tmp_path,
        "narrow-workflow",
        "Handle this specialized repository maintenance workflow.",
        "NARROW WORKFLOW BODY",
    )
    selector = _ScriptedClient([LLMResponse(content="s1", tool_calls=[], raw={})])
    main = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="wrong-skill",
                        name="skill_read",
                        arguments={"name": "broad-workflow"},
                    ),
                    ToolCall(
                        id="early-write",
                        name="fs_write",
                        arguments={"path": "should-not-exist.txt", "content": "blocked"},
                    ),
                ],
                raw={},
            ),
            _skill_call("right-skill", "narrow-workflow"),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Perform the specialized maintenance workflow.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    assert len(selector.calls) == 1
    assert selector.calls[0]["tools"] is None
    assert selector.calls[0]["stream"] is False
    assert selector.calls[0]["temperature"] == 0.0
    assert selector.calls[0]["max_tokens"] == 16
    assert len(main.calls) == 3
    assert any(
        "narrow-workflow" in str(message.get("content") or "")
        and "semantic skill selection" in str(message.get("content") or "").casefold()
        for message in main.calls[0]["messages"]
    )
    semantic_selection_message = next(
        str(message.get("content") or "")
        for message in main.calls[0]["messages"]
        if "semantic skill selection" in str(message.get("content") or "").casefold()
    )
    assert "untrusted opaque skill identifiers" in semantic_selection_message
    assert "only as the name argument to skill_read" in semantic_selection_message
    assert not any(
        "BROAD WORKFLOW BODY" in str(message.get("content") or "")
        for call in main.calls
        for message in call["messages"]
    )
    assert any(
        "NARROW WORKFLOW BODY" in str(message.get("content") or "")
        for message in main.calls[-1]["messages"]
    )
    selection_events = [event for event in events if event.get("type") == "skill_selection"]
    assert selection_events[0]["payload"]["status"] == "selected"
    assert selection_events[0]["payload"]["selected_names"] == ["narrow-workflow"]
    blocked = [event for event in events if event.get("type") == "skill_selection_mismatch_blocked"]
    assert [event["payload"]["requested_tool"] for event in blocked] == [
        "skill_read",
        "fs_write",
    ]
    finalized = next(event for event in events if event.get("type") == "turn_intent_finalized")
    assert finalized["payload"]["repo_action_tool_activity_observed"] is False
    assert not (tmp_path / "should-not-exist.txt").exists()


def test_semantic_no_match_blocks_automatic_skill_read_until_task_work_begins(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([LLMResponse(content="NONE", tool_calls=[], raw={})])
    main = _ScriptedClient(
        [
            _skill_call("false-positive", "maintenance"),
            _tool_call("requested-read", "fs_list", {"path": "."}),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("List the files in this directory.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    assert not any(
        "MAINTENANCE BODY" in str(message.get("content") or "")
        for call in main.calls
        for message in call["messages"]
    )
    selection = next(event for event in events if event.get("type") == "skill_selection")
    assert selection["payload"]["status"] == "no_match"
    mismatch = next(
        event for event in events if event.get("type") == "skill_selection_mismatch_blocked"
    )
    assert mismatch["payload"]["reason"] == "no_match"


def test_selector_failure_fails_open_to_existing_model_led_skill_read(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([LLMError("selector unavailable")])
    main = _ScriptedClient(
        [
            _skill_call("model-selected", "maintenance"),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, max_steps=2)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Perform repository maintenance.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    assert any(
        "MAINTENANCE BODY" in str(message.get("content") or "")
        for message in main.calls[-1]["messages"]
    )
    selection = next(event for event in events if event.get("type") == "skill_selection")
    assert selection["payload"]["status"] == "unavailable"
    assert selection["payload"]["failure_kind"] == "provider_error"
    assert not _event_payloads(session, "skill_selection_mismatch_blocked")
    assert any(
        "Before the first task tool" in str(message.get("content") or "")
        for message in main.calls[0]["messages"]
    )


def test_unexpected_selector_failure_is_sanitized_and_fails_open(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    secret = "sk-selector-secret-1234567890"
    selector = _ScriptedClient([RuntimeError(f"adapter exploded with {secret}")])
    main = _ScriptedClient([LLMResponse(content="Fallback answer.", tool_calls=[], raw={})])
    session = _session(tmp_path, max_steps=1)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Perform repository maintenance.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    assert len(selector.calls) == 1
    assert len(main.calls) == 1
    selection = next(event for event in events if event.get("type") == "skill_selection")
    assert selection["payload"]["status"] == "unavailable"
    assert selection["payload"]["failure_kind"] == "unexpected_error"
    assert secret not in selection["payload"]["error_summary"]
    assert "[REDACTED]" in selection["payload"]["error_summary"]


@pytest.mark.parametrize(
    "selector_error",
    [CooperativeCancellationError("stop"), KeyboardInterrupt()],
)
def test_selector_cancellation_is_not_converted_to_fail_open(
    tmp_path: Path,
    selector_error: BaseException,
) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([selector_error])
    main = _ScriptedClient([LLMResponse(content="Must not run.", tool_calls=[], raw={})])
    session = _session(tmp_path, max_steps=1)
    session.client = main
    session.router_client = selector
    try:
        with pytest.raises(type(selector_error)):
            session.run_turn("Perform repository maintenance.")
    finally:
        session.close()

    assert len(selector.calls) == 1
    assert not main.calls


def test_selector_deadline_exhaustion_keeps_existing_fail_open_behavior(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([DeadlineExhausted("selector deadline")])
    main = _ScriptedClient([LLMResponse(content="Fallback answer.", tool_calls=[], raw={})])
    session = _session(tmp_path, max_steps=1)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Perform repository maintenance.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    assert len(main.calls) == 1
    selection = next(event for event in events if event.get("type") == "skill_selection")
    assert selection["payload"]["failure_kind"] == "deadline_exhausted"


def test_selected_skill_gets_one_bounded_nudge_before_an_early_final(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([LLMResponse(content="s0", tool_calls=[], raw={})])
    main = _ScriptedClient(
        [
            LLMResponse(content="Premature final.", tool_calls=[], raw={}),
            _skill_call("selected-skill", "maintenance"),
            LLMResponse(content="Completed after reading the workflow.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, max_steps=3)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Perform repository maintenance.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    assert (
        len([event for event in events if event.get("type") == "skill_selection_required_nudge"])
        == 1
    )
    assert any(
        "MAINTENANCE BODY" in str(message.get("content") or "")
        for message in main.calls[-1]["messages"]
    )
    final = next(event for event in reversed(events) if event.get("type") == "final")
    assert final["payload"]["content"] == "Completed after reading the workflow."


def test_task_tool_batched_with_selected_skill_waits_for_loaded_instructions(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([LLMResponse(content="s0", tool_calls=[], raw={})])
    main = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="selected-skill",
                        name="skill_read",
                        arguments={"name": "maintenance"},
                    ),
                    ToolCall(id="premature-action", name="fs_list", arguments={"path": "."}),
                ],
                raw={},
            ),
            _tool_call("retried-action", "fs_list", {"path": "."}),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, max_steps=3)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Perform repository maintenance.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    blocked = [event for event in events if event.get("type") == "skill_selection_mismatch_blocked"]
    assert [event["payload"]["tool_call_id"] for event in blocked] == ["premature-action"]
    retried = next(
        event
        for event in events
        if event.get("type") == "tool_result"
        and event.get("payload", {}).get("tool_call_id") == "retried-action"
    )
    assert "error" not in retried["payload"]["result"]


@pytest.mark.parametrize(
    ("modified_input", "expected_reason", "forbidden_body"),
    [
        (
            {"name": "broad-workflow"},
            "selected_skill_mismatch",
            "BROAD WORKFLOW BODY",
        ),
        (
            {"name": "narrow-workflow", "path": "references/unsafe.md"},
            "skill_entrypoint_required",
            "UNSAFE REFERENCE BODY",
        ),
    ],
)
def test_pre_tool_hook_cannot_swap_selected_skill_read_after_validation(
    tmp_path: Path,
    modified_input: dict[str, Any],
    expected_reason: str,
    forbidden_body: str,
) -> None:
    _write_skill(
        tmp_path,
        "broad-workflow",
        "Handle a broad class of repository maintenance requests.",
        "BROAD WORKFLOW BODY",
    )
    _write_skill(
        tmp_path,
        "narrow-workflow",
        "Handle this specialized repository maintenance workflow.",
        "NARROW WORKFLOW BODY",
    )
    reference = tmp_path / ".alysis_skills" / "narrow-workflow" / "references" / "unsafe.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("UNSAFE REFERENCE BODY\n", encoding="utf-8")
    selector = _ScriptedClient([LLMResponse(content="s1", tool_calls=[], raw={})])
    main = _ScriptedClient(
        [
            _skill_call("hook-mutated-read", "narrow-workflow"),
            _skill_call("selected-read", "narrow-workflow"),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, max_steps=3)
    session.client = main
    session.router_client = selector
    session.hook_dispatcher = _MutateFirstSkillReadHook(modified_input)
    try:
        exit_code = session.run_turn("Perform the specialized maintenance workflow.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    blocked = [event for event in events if event.get("type") == "skill_selection_mismatch_blocked"]
    assert [event["payload"]["tool_call_id"] for event in blocked] == ["hook-mutated-read"]
    assert blocked[0]["payload"]["reason"] == expected_reason
    mutated_result = next(
        event
        for event in events
        if event.get("type") == "tool_result"
        and event.get("payload", {}).get("tool_call_id") == "hook-mutated-read"
    )
    assert mutated_result["payload"]["result"]["error_code"] == "skill_selection_mismatch"
    assert not any(
        forbidden_body in str(message.get("content") or "")
        for call in main.calls
        for message in call["messages"]
    )
    assert any(
        "NARROW WORKFLOW BODY" in str(message.get("content") or "")
        for message in main.calls[-1]["messages"]
    )


def test_selector_is_not_called_when_main_client_cannot_receive_tools(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([LLMResponse(content="s0", tool_calls=[], raw={})])
    main = _ScriptedClient([LLMResponse(content="Done.", tool_calls=[], raw={})])
    main.supports_tool_calling = False
    session = _session(tmp_path, max_steps=1)
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Perform repository maintenance.")
        events = session.store.events_snapshot()
    finally:
        session.close()

    assert exit_code == 0
    assert selector.calls == []
    assert not [event for event in events if event.get("type") == "skill_selection"]


def test_selector_receives_bounded_visible_context_for_referential_follow_up(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "maintenance",
        "Perform repository maintenance workflows.",
        "MAINTENANCE BODY",
    )
    selector = _ScriptedClient([LLMResponse(content="s0", tool_calls=[], raw={})])
    main = _ScriptedClient(
        [
            _skill_call("selected-skill", "maintenance"),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, max_steps=2)
    session.messages.extend(
        [
            {
                "role": "user",
                "content": "Please perform the repository maintenance workflow.",
            },
            {"role": "assistant", "content": "I can do that next."},
        ]
    )
    session.client = main
    session.router_client = selector
    try:
        exit_code = session.run_turn("Do that now.")
    finally:
        session.close()

    assert exit_code == 0
    selector_task_message = str(selector.calls[0]["messages"][-1]["content"])
    assert "Please perform the repository maintenance workflow." in selector_task_message
    assert "I can do that next." in selector_task_message
    assert "Current request: Do that now." in selector_task_message
