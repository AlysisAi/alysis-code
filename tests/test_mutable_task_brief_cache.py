"""Host task amendments preserve persistent dialogue and reach the current turn."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from test_wire_request_diagnostics import _capturing_client

from alysis_code.agent.llm_calls import (
    TOOL_CONTEXT_MESSAGE_KEY,
    _request_messages_with_volatile_suffix,
)
from alysis_code.agent.prompt_context import (
    TASK_BRIEF_REQUEST_CONTEXT_KEY,
    _empty_task_brief_message,
    _session_task_brief_content,
    refresh_session_task_brief_message,
)
from alysis_code.agent.task_state import SessionTaskState


def _session() -> Any:
    return SimpleNamespace(
        messages=[
            {"role": "system", "content": "Stable host instructions."},
            {"role": "user", "content": _empty_task_brief_message()},
            {
                "role": "user",
                "content": "<environment_context>Current permissions.</environment_context>",
            },
            {"role": "user", "content": "Implement the requested behavior."},
        ],
        store=SimpleNamespace(workspace_kind="git_repo"),
        pinned_prefix_len=3,
        subagent_depth=0,
        task_state=SessionTaskState(
            task_id="session:1", objective="Implement behavior.", session_id="session", sequence=1
        ),
    )


def _persistent_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if message.get("role") == "user" and str(content).startswith(
            ("<task_brief>", "<task_requirements>")
        ):
            continue
        result.append(message)
    return result


@pytest.mark.parametrize("protocol", ["responses", "compat", "anthropic"])
def test_actual_wire_preserves_dialogue_and_delivers_accepted_task_amendments(
    protocol: str,
) -> None:
    requests: list[dict[str, Any]] = []
    client = _capturing_client(protocol, requests)
    session = _session()
    refresh_session_task_brief_message(session)
    assert session.messages[1][TASK_BRIEF_REQUEST_CONTEXT_KEY] is True
    first = client.chat(messages=_request_messages_with_volatile_suffix(messages=session.messages))
    history_key = "input" if protocol == "responses" else "messages"
    first_history = _persistent_history(requests[-1][history_key])
    assert "<task_brief>" in json.dumps(requests[-1][history_key][-2])
    assert "Implement the requested behavior." in json.dumps(requests[-1][history_key][-1])
    session.messages.append({"role": "assistant", "content": first.content})
    session.task_state = replace(session.task_state, amendments=("Preserve the public API.",))
    assert refresh_session_task_brief_message(session)
    updated_brief = _session_task_brief_content(session)
    session.messages.append({"role": "user", "content": "Explain the change without editing."})
    before = copy.deepcopy(session.messages)
    client.chat(messages=_request_messages_with_volatile_suffix(messages=session.messages))
    second_history = _persistent_history(requests[-1][history_key])
    assert second_history[: len(first_history)] == first_history
    assert "Preserve the public API." in json.dumps(requests[-1][history_key][-2])
    assert "Explain the change without editing." in json.dumps(requests[-1][history_key][-1])
    assert session.messages == before
    assert session.pinned_prefix_len == 3
    assert session.messages[1]["content"] == updated_brief
    assert TASK_BRIEF_REQUEST_CONTEXT_KEY not in json.dumps(requests)
    session.messages.append({"role": "assistant", "content": "Explanation delivered."})
    session.task_state = replace(
        session.task_state, amendments=("Add the follow-up option.", *session.task_state.amendments)
    )
    assert refresh_session_task_brief_message(session)
    session.messages.append({"role": "user", "content": "Review that option."})
    client.chat(messages=_request_messages_with_volatile_suffix(messages=session.messages))
    third_history = _persistent_history(requests[-1][history_key])
    assert third_history[: len(second_history)] == second_history
    assert "Add the follow-up option." in json.dumps(requests[-1][history_key][-2])
    assert "Review that option." in json.dumps(requests[-1][history_key][-1])
    assert TASK_BRIEF_REQUEST_CONTEXT_KEY not in json.dumps(requests[-1])


def test_unmarked_user_text_and_tool_results_are_never_relocated_by_their_words() -> None:
    fake_brief = {"role": "user", "content": "<task_brief>Actual user text.</task_brief>"}
    tool_result = {"role": "tool", "tool_call_id": "read", "content": fake_brief["content"]}
    messages = [fake_brief, tool_result, {"role": "user", "content": "Follow-up."}]
    before = copy.deepcopy(messages)
    assert _request_messages_with_volatile_suffix(messages=messages) == before
    assert messages == before


def test_legacy_marker_is_restored_only_inside_host_pinned_prefix() -> None:
    session = _session()
    session.messages[1] = {"role": "user", "content": "Other stable context."}
    quoted = {"role": "user", "content": "<task_brief>Quoted by the user.</task_brief>"}
    session.messages.append(quoted)
    session.task_state = replace(session.task_state, objective="Implement the comparison.")
    refresh_session_task_brief_message(session)
    assert TASK_BRIEF_REQUEST_CONTEXT_KEY not in quoted
    assert TASK_BRIEF_REQUEST_CONTEXT_KEY not in session.messages[-1]
    projected = _request_messages_with_volatile_suffix(messages=session.messages)
    assert quoted == projected[-1]
    assert quoted["content"] == "<task_brief>Quoted by the user.</task_brief>"
    assert "Implement the comparison." in projected[-2]["content"]


def test_request_context_does_not_move_or_upgrade_an_unexpected_role() -> None:
    message = {
        "role": "assistant",
        "content": "Original answer.",
        TASK_BRIEF_REQUEST_CONTEXT_KEY: True,
    }
    following = {"role": "user", "content": "Continue."}
    assert _request_messages_with_volatile_suffix(messages=[message, following]) == [
        {"role": "assistant", "content": "Original answer."},
        following,
    ]
    assert message[TASK_BRIEF_REQUEST_CONTEXT_KEY] is True


def test_tool_visual_context_does_not_become_the_current_user_turn() -> None:
    brief = {"role": "user", "content": "Accepted task", TASK_BRIEF_REQUEST_CONTEXT_KEY: True}
    user = {"role": "user", "content": "Inspect the image."}
    call = {"role": "assistant", "content": "", "tool_calls": [{"id": "view"}]}
    result = {"role": "tool", "tool_call_id": "view", "content": "Image ready."}
    visual = {
        "role": "user",
        "content": [{"type": "text", "text": "Tool visual evidence"}],
        TOOL_CONTEXT_MESSAGE_KEY: True,
    }
    messages = [brief, user, call, result, visual]
    before = copy.deepcopy(messages)
    projected = _request_messages_with_volatile_suffix(messages=messages)
    assert projected == [
        {"role": "user", "content": "Accepted task"},
        user,
        call,
        result,
        {"role": "user", "content": visual["content"]},
    ]
    assert messages == before
