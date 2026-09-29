from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop_same_batch_read_dedup import _ScriptedClient
from test_subagents import _build_main_tools, _FakeSubSession, _RecordingStore

from alysis_code import agent_loop
from alysis_code import cli as cli_mod
from alysis_code.agent.llm_calls import _registered_tool_schema_list
from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.subagents import SubagentDefinition, built_in_subagents


def _names(schemas: list[dict[str, Any]]) -> set[str]:
    return {schema["function"]["name"] for schema in schemas}


def _session(tmp_path: Path, *, mode: str = "readonly", non_interactive: bool = False):
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", skills_enabled=False),
        root=tmp_path,
        mode=mode,
        non_interactive=non_interactive,
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        enable_tool_output_offload=False,
        enable_conversation_summarization=False,
        verification_enabled=False,
    )


@pytest.mark.parametrize("mode", ["readonly", "fullaccess"])
@pytest.mark.parametrize("non_interactive", [False, True])
def test_real_parent_has_one_range_capable_read_tool(
    tmp_path: Path, mode: str, non_interactive: bool
) -> None:
    session = _session(tmp_path, mode=mode, non_interactive=non_interactive)
    try:
        assert {"fs_read", "fs_list"} <= set(session.tools)
        assert "fs_read_lines" not in session.tools
        assert {"fs_read", "fs_list"} <= _names(session.tool_list)
        assert "fs_read_lines" not in _names(session.tool_list)
        schema = next(
            t["function"] for t in session.tool_list if t["function"]["name"] == "fs_read"
        )
        assert {"start_line", "end_line", "max_lines", "include_line_numbers"} <= set(
            schema["parameters"]["properties"]
        )
        assert schema["parameters"]["required"] == ["path"]
    finally:
        session.close()


@pytest.mark.parametrize(
    ("role", "allow", "deny", "expected_read"),
    [
        ("general", None, (), True),
        ("explorer", None, (), True),
        ("code-reviewer", None, (), True),
        ("custom-reader", ("fs_read",), (), True),
        ("custom-reader", ("fs_read", "fs_list"), ("fs_read",), False),
        ("custom-reader", ("fs_list",), (), False),
    ],
)
def test_child_read_scope_and_catalog_match_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    allow: tuple[str, ...] | None,
    deny: tuple[str, ...],
    expected_read: bool,
) -> None:
    # Use real builtins and the real launcher/scope resolution; only child model
    # execution is replaced. Allow and deny rules still determine read access.
    child = _FakeSubSession(
        tools=_build_main_tools(tmp_path=tmp_path, subagents_enabled=False, mode="readonly")
    )
    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: child)
    registry = built_in_subagents()
    if allow is not None:
        registry[role] = SubagentDefinition(
            name=role,
            description="Read the assigned source.",
            system_prompt="Preserve the configured tool scope.",
            prompt_trust="untrusted",
            allow_tools=allow,
            deny_tools=deny,
        )
    store = _RecordingStore()
    tools = _build_main_tools(
        tmp_path=tmp_path,
        subagents_enabled=True,
        subagent_registry=registry,
        store=store,
    )
    result = tools["subagent_run"].run(
        {"name": role, "task": "Inspect the assigned source.", "mode": "readonly"}
    )
    assert result["result"] == "subagent final"
    assert "fs_read_lines" not in child.tools
    assert "fs_read_lines" not in _names(child.tool_list)
    assert ("fs_read" in child.tools) == expected_read
    assert ("fs_read" in _names(child.tool_list)) == expected_read
    catalog = next(payload for kind, payload in store.events if kind == "subagent_tool_catalog")
    assert set(catalog["tool_names"]) == _names(child.tool_list)
    catalog_message = child.messages[-1]["content"]
    assert "fs_read_lines" not in catalog_message
    assert ("- fs_read:" in catalog_message) == expected_read
    assert not {"fs_write", "fs_edit", "shell_run"}.intersection(child.tools)
    if expected_read:
        (tmp_path / "child.txt").write_text("one\ntwo\n", encoding="utf-8")
        result = child.tools["fs_read"].run({"path": "child.txt", "start_line": 2, "end_line": 2})
        assert result["content"] == "2: two\n"


def test_rebuilt_request_drops_removed_schema_without_mutating_saved_schema(
    tmp_path: Path,
) -> None:
    tools = _build_main_tools(tmp_path=tmp_path, subagents_enabled=False)
    stale = [
        {
            "type": "function",
            "function": {"name": "fs_read_lines", "parameters": {"type": "object"}},
        },
        tools["fs_list"].as_openai_tool(),
    ]
    before = copy.deepcopy(stale)
    rebuilt = _registered_tool_schema_list(tools, stale)
    assert "fs_read_lines" not in _names(rebuilt)
    assert "fs_read" in _names(rebuilt)
    assert rebuilt[0]["function"]["name"] == "fs_list"
    assert stale == before
    assert "fs_read_lines" not in tools

    without_read = {name: tool for name, tool in tools.items() if name != "fs_read"}
    assert not {"fs_read", "fs_read_lines"}.intersection(
        _names(_registered_tool_schema_list(without_read, rebuilt))
    )


@pytest.mark.parametrize("allow", [("fs_read_lines",), ("fs_read_lines", "fs_list")])
def test_old_custom_read_allowlist_fails_clearly_without_granting_canonical_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow: tuple[str, ...]
) -> None:
    child = _FakeSubSession(
        tools=_build_main_tools(tmp_path=tmp_path, subagents_enabled=False, mode="readonly")
    )
    monkeypatch.setattr(agent_loop, "create_session", lambda **_kwargs: child)
    definition = SubagentDefinition(
        name="old-reader",
        description="Read the assigned source.",
        system_prompt="Use only the configured tools.",
        prompt_trust="untrusted",
        allow_tools=allow,
    )
    tools = _build_main_tools(
        tmp_path=tmp_path, subagents_enabled=True, subagent_registry={definition.name: definition}
    )
    result = tools["subagent_run"].run({"name": definition.name, "task": "Inspect the source."})
    assert result["status"] == "failed"
    assert result["error_code"] == "subagent_allowlist_unavailable"
    assert result["unavailable_allowed_tools"] == ["fs_read_lines"]
    assert "fs_read_lines" in result["error"]
    assert "fs_read" not in result["resolved_allowed_tools"]
    assert child.run_calls == []
    assert child.closed is True


def test_mode_rebuild_preserves_history_without_restoring_removed_tool(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    try:
        session.messages.append({"role": "user", "content": "Keep my existing changes."})
        before = copy.deepcopy(session.messages)
        cli_mod._rebuild_session_tools_for_mode(session=session, mode="fullaccess")
        assert "fs_read_lines" not in session.tools
        assert "fs_read_lines" not in _names(session.tool_list)
        assert "fs_read" in _names(session.tool_list)
        assert session.messages == before
    finally:
        session.close()


def test_resumed_legacy_history_is_unchanged_and_new_old_name_uses_unknown_tool_recovery(
    tmp_path: Path,
) -> None:
    (tmp_path / "demo.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    historical_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "old-range",
                "type": "function",
                "function": {
                    "name": "fs_read_lines",
                    "arguments": '{"path":"demo.txt","start_line":1,"end_line":1}',
                },
            }
        ],
    }
    events = [
        {"type": "user_message", "payload": {"content": "Read the first line."}},
        {"type": "assistant_message", "payload": {"message": historical_call}},
        {"type": "tool_result", "payload": {"tool_call_id": "old-range", "content": "1: one"}},
        {"type": "final", "payload": {"content": "The first line is one."}},
    ]
    log = tmp_path / "prior.jsonl"
    log.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    restored = cli_mod._load_chat_resume_messages(log)
    assert restored[1] == historical_call
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="new-range",
                        name="fs_read_lines",
                        arguments={"path": "demo.txt", "start_line": 2, "end_line": 3},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="canonical-range",
                        name="fs_read",
                        arguments={"path": "demo.txt", "start_line": 2, "end_line": 3},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="The remaining lines are two and three.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path)
    session.client = client
    session.messages.extend(copy.deepcopy(restored))
    try:
        assert session.run_turn("Read the remaining two lines.") == 0
        assert len(client.calls) == 3
        assert all("fs_read_lines" not in _names(call["tools"]) for call in client.calls)
        assert historical_call in session.messages
        failed_result = next(
            message
            for message in session.messages
            if message.get("tool_call_id") == "new-range" and message.get("role") == "tool"
        )
        failure = json.loads(failed_result["content"])
        assert failure["error_code"] == "unknown_tool"
        assert failure["safe_compatibility_alias"] is False
        assert "content" not in failure
        assert "fs_read" in failure["available_tool_names"]
        result = next(
            message
            for message in session.messages
            if message.get("tool_call_id") == "canonical-range" and message.get("role") == "tool"
        )
        content = json.loads(result["content"])
        assert content["content"] == "2: two\n3: three\n"
        assert content["start_line"] == 2
        assert content["end_line"] == 3
    finally:
        session.close()
