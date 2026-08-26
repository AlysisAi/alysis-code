from __future__ import annotations

import json

from alysis_code.plan_mode import (
    PLAN_MODE_SYSTEM_PROMPT,
    TOOL_RESULT_SUMMARY_CHARS,
    build_plan_context_messages,
)


def _user_prompt(messages: list[dict[str, object]]) -> str:
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    return str(messages[1]["content"] or "")


def _transcript_entries(prompt: str) -> list[dict[str, str]]:
    marker = "Recent conversation transcript JSON:\n"
    assert marker in prompt
    after_marker = prompt.split(marker, 1)[1]
    if "\n\nWorkspace context JSON:\n" in after_marker:
        transcript_json = after_marker.split("\n\nWorkspace context JSON:\n", 1)[0]
    else:
        transcript_json = after_marker.split("\n\nDraft an execution plan for this request:\n", 1)[
            0
        ]
    raw_entries = json.loads(transcript_json)
    return [
        {
            "role": str(entry.get("role") or ""),
            "content": str(entry.get("content") or ""),
        }
        for entry in raw_entries
        if isinstance(entry, dict)
    ]


def test_build_plan_context_messages_always_returns_system_and_user() -> None:
    messages = build_plan_context_messages(
        session_messages=[
            {"role": "system", "content": "ignore me"},
            {"role": "user", "content": "Earlier user turn"},
            {"role": "assistant", "content": "Earlier assistant turn"},
        ],
        user_message="Implement the parser fix",
    )

    assert messages == [
        {"role": "system", "content": PLAN_MODE_SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(messages)},
    ]


def test_plan_context_serializes_history_workspace_and_latest_request_into_single_user_prompt() -> (
    None
):
    workspace_context = {
        "workspace_kind": "git_repo",
        "focus_relpath": ".",
        "top_level_entries": [{"path": "src", "kind": "dir"}],
        "manifests": [{"path": "pyproject.toml", "kind": "python"}],
        "likely_test_commands": ["pytest -q"],
    }
    messages = build_plan_context_messages(
        session_messages=[
            {"role": "system", "content": "ignore me"},
            {"role": "user", "content": "Earlier request: inspect parser"},
            {"role": "assistant", "content": "Earlier answer: parser likely in src/parser.py"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fs_read", "arguments": '{"path":"README.md"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "README excerpt"},
        ],
        user_message="Implement the parser fix",
        workspace_context=workspace_context,
    )

    prompt = _user_prompt(messages)

    assert "Earlier request: inspect parser" in prompt
    assert "Earlier answer: parser likely in src/parser.py" in prompt
    assert "Assistant requested tool calls: fs_read." in prompt
    assert "Tool result (fs_read): README excerpt" in prompt
    assert "Workspace context JSON:" in prompt
    assert '"likely_test_commands": ["pytest -q"]' in prompt
    assert "Draft an execution plan for this request:\nImplement the parser fix" in prompt


def test_plan_context_keeps_tool_results_as_textual_tool_transcript_entries() -> None:
    messages = build_plan_context_messages(
        session_messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fs_read", "arguments": '{"path":"README.md"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "first result"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "run_tests", "arguments": '{"cmd":"pytest -q"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": "second result"},
            {"role": "assistant", "content": "Done reading context."},
        ],
        user_message="Draft the fix plan",
    )

    transcript_entries = _transcript_entries(_user_prompt(messages))

    assert [message["role"] for message in messages] == ["system", "user"]
    assert transcript_entries == [
        {"role": "assistant", "content": "Assistant requested tool calls: fs_read."},
        {"role": "tool", "content": "Tool result (fs_read): first result"},
        {"role": "assistant", "content": "Assistant requested tool calls: run_tests."},
        {"role": "tool", "content": "Tool result (run_tests): second result"},
        {"role": "assistant", "content": "Done reading context."},
    ]


def test_plan_context_includes_previous_draft_and_feedback() -> None:
    messages = build_plan_context_messages(
        session_messages=[],
        user_message="Refine the CLI flow",
        previous_plan="1. Update cli.py",
        feedback="Please include tests.",
    )

    prompt = _user_prompt(messages)

    assert "Previous draft plan:\n1. Update cli.py" in prompt
    assert "User feedback:\nPlease include tests." in prompt
    assert "Revise this previous draft plan based on user feedback." in prompt


def test_plan_context_truncates_tool_results_and_caps_normalized_history_entries() -> None:
    long_tool_output = "X" * (TOOL_RESULT_SUMMARY_CHARS + 50)
    messages = build_plan_context_messages(
        session_messages=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fs_read", "arguments": '{"path":"README.md"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": long_tool_output},
            {"role": "assistant", "content": "a2"},
        ],
        user_message="Draft the plan",
        max_history_messages=2,
    )

    prompt = _user_prompt(messages)
    transcript_entries = _transcript_entries(prompt)

    assert transcript_entries == [
        {
            "role": "tool",
            "content": f"Tool result (fs_read): {'X' * TOOL_RESULT_SUMMARY_CHARS}...",
        },
        {"role": "assistant", "content": "a2"},
    ]
    assert ("X" * (TOOL_RESULT_SUMMARY_CHARS + 1)) not in prompt
