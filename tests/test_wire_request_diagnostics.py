from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import httpx
import pytest
from test_openai_codex_instruction_prefix import _OfflineSubscriptionAuth

from alysis_code.agent.llm_calls import _request_messages_with_volatile_suffix
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.request_plan import WireRequestDiagnostics
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)


@pytest.fixture(autouse=True)
def isolated_telemetry() -> Any:
    reset_provider_telemetry_for_tests()
    yield
    reset_provider_telemetry_for_tests()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_wire_diagnostics_detect_moved_history_and_retries_without_retaining_content() -> None:
    tracker = WireRequestDiagnostics()
    first = {
        "instructions": "private policy",
        "input": [{"role": "user", "content": "private user"}, {"opaque": "private opaque"}],
        "tools": [{"name": "private_tool"}],
    }
    original = copy.deepcopy(first)
    describe = tracker.begin_request()
    initial = describe(first, history_key="input", instructions_key="instructions")
    assert "wire_previous_history_items" not in initial
    second = copy.deepcopy(first)
    second["input"].insert(1, {"role": "assistant", "content": "new reply"})
    describe = tracker.begin_request()
    moved = describe(second, history_key="input", instructions_key="instructions")
    assert moved["wire_shared_prefix_items"] == 1
    assert moved["wire_previous_history_items"] == 2
    assert moved["wire_previous_history_prefix_preserved"] is False
    assert moved["wire_previous_instructions_unchanged"] is True
    assert moved["wire_history_sha256"] == _digest(second["input"])
    # Re-preparing a fallback does not silently compare the call to itself.
    second["tools"] = []
    retry = describe(second, history_key="input", instructions_key="instructions")
    assert retry["wire_shared_prefix_items"] == 1
    assert retry["wire_previous_history_items"] == 2
    assert retry["wire_previous_tools_unchanged"] is False
    second["input"].append({"role": "user", "content": "follow-up"})
    third = tracker.begin_request()(second, history_key="input", instructions_key="instructions")
    assert third["wire_previous_history_items"] == 3
    assert third["wire_previous_history_prefix_preserved"] is True
    assert first == original
    for forbidden in ("private policy", "private user", "private opaque", "private_tool"):
        assert forbidden not in json.dumps([initial, moved, retry, third])
        assert forbidden not in repr(tracker.__dict__)


def _capturing_client(protocol: str, requests: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 1})
        requests.append(json.loads(request.content))
        if protocol == "responses":
            event = {
                "type": "response.completed",
                "response": {
                    "id": f"local-{len(requests)}",
                    "model": "test-model",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Local reply."}],
                        }
                    ],
                },
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
            )
        if protocol == "anthropic":
            return httpx.Response(
                200,
                json={
                    "id": "local",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [{"type": "text", "text": "Local reply."}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "local",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Local reply."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    common = {"api_key": "", "model": "test-model", "transport": httpx.MockTransport(handler)}
    if protocol == "responses":
        return OpenAIResponsesClient(
            base_url="https://chatgpt.com/backend-api/codex",
            provider_auth=_OfflineSubscriptionAuth(),
            **common,
        )
    if protocol == "anthropic":
        return AnthropicMessagesClient(base_url="https://example.test/v1", **common)
    return OpenAICompatClient(base_url="https://example.test/v1", **common)


@pytest.mark.parametrize("protocol", ["responses", "anthropic", "compat"])
def test_captured_wire_keeps_persistent_context_order_across_turns(protocol: str) -> None:
    requests: list[dict[str, Any]] = []
    client = _capturing_client(protocol, requests)
    messages = [
        {"role": "system", "content": "Stable host authority."},
        {
            "role": "user",
            "content": "<workspace_binding_context>Workspace one.</workspace_binding_context>",
        },
        {"role": "user", "content": "<task_brief>Read the requested source.</task_brief>"},
        {
            "role": "user",
            "content": "<environment_context>Read-only permission.</environment_context>",
        },
        {"role": "user", "content": "First task."},
    ]
    for index in range(3):
        response = client.chat(messages=_request_messages_with_volatile_suffix(messages=messages))
        recorded = last_provider_call_summary()["request_plan"]
        history_key = "input" if protocol == "responses" else "messages"
        assert recorded["wire_history_sha256"] == _digest(requests[-1][history_key])
        assert recorded["wire_history_items"] == len(requests[-1][history_key])
        if index:
            assert recorded["wire_previous_history_prefix_preserved"] is True
        else:
            assert "wire_previous_history_items" not in recorded
        messages.extend(
            [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": f"Follow-up {index}."},
            ]
        )
    history_key = "input" if protocol == "responses" else "messages"
    instructions_key = "instructions" if protocol == "responses" else "system"
    for previous, current in zip(requests, requests[1:], strict=False):
        assert _digest(current[history_key][: len(previous[history_key])]) == _digest(
            previous[history_key]
        )
        assert current.get(instructions_key) == previous.get(instructions_key)
    # A genuine host permission/context refresh must reach the next request.
    messages[3]["content"] = (
        "<environment_context>Updated read-only permission: no web.</environment_context>"
    )
    client.chat(
        messages=_request_messages_with_volatile_suffix(
            messages=messages, step_system_prompts=["Current host instruction: do not write files."]
        )
    )
    encoded = json.dumps(requests[-1])
    assert "Updated read-only permission: no web." in encoded
    assert "Current host instruction: do not write files." in encoded
    assert "Read-only permission.</environment_context>" not in encoded
    if protocol == "responses":
        assert requests[-1]["input"][-1]["role"] == "developer"
        assert requests[-1]["instructions"] == requests[0]["instructions"]
    elif protocol == "compat":
        assert requests[-1]["messages"][-1]["role"] == "system"
    else:
        assert "Current host instruction" in json.dumps(requests[-1]["system"])


@pytest.mark.parametrize("protocol", ["responses", "anthropic", "compat"])
def test_native_successful_child_resume_keeps_recorded_history_order(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    from test_agent_loop_event_emission import _make_session, _RecordingEventSurface
    from test_subagents import _build_main_tools, _readonly_subagent_tools

    from alysis_code import agent_loop
    from alysis_code.session_store import SessionStore
    from alysis_code.subagents import SubagentDefinition

    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    requests: list[dict[str, Any]] = []
    created = []

    def create_child(**kwargs: Any) -> Any:
        session = _make_session(
            root=kwargs["root"],
            client=_capturing_client(protocol, requests),
            surface=_RecordingEventSurface(),
            tool=_readonly_subagent_tools()["fs_read"],
        )
        session.store = SessionStore(
            enabled=True,
            sessions_dir=tmp_path / "child-sessions",
            session_id=f"child-{len(created)}",
            cwd=str(tmp_path),
            repo_root=str(tmp_path),
        )
        session.messages.append(
            {
                "role": "user",
                "content": "<workspace_binding_context>Same workspace.</workspace_binding_context>",
            }
        )
        created.append(session)
        return session

    monkeypatch.setattr(agent_loop, "create_session", create_child)
    tools = _build_main_tools(
        tmp_path=tmp_path,
        subagents_enabled=True,
        subagent_registry={
            "reader": SubagentDefinition(
                name="reader",
                description="Read assigned source",
                system_prompt="Read source only.",
                mode="readonly",
                allow_tools=("fs_read",),
                allow_workspace_writes=False,
            ),
        },
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        first = tools["subagent_run"].run({"name": "reader", "task": "Inspect first source."})
        assert first.get("status") == "success", first
        followup = tools["subagent_resume"].run(
            {"run_id": first["run_id"], "task": "Inspect second source."}
        )
        child = scheduler._children[followup["run_id"]]
        child.completion.result(timeout=5)
        child.worker_bookkeeping_completion.result(timeout=5)
        assert (
            tools["subagent_wait"].run({"run_id": followup["run_id"]})["results"][
                followup["run_id"]
            ]["status"]
            == "success"
        )
    finally:
        scheduler.shutdown(cancel_pending=True)
    assert len(created) == len(requests) == 2
    key = "input" if protocol == "responses" else "messages"
    # Fresh child construction must not move the initial binding behind restored turns.
    prefix_length = 2 if protocol == "compat" else 1
    assert _digest(requests[1][key][:prefix_length]) == _digest(requests[0][key][:prefix_length])
    # Resume reconstructs fresh host notices, so the entire old wire request need
    # not be a prefix. Restored task/report content still keeps its native order.
    serialized = json.dumps(requests[1][key])
    assert serialized.index("Same workspace.") < serialized.index("Inspect first source.")
    assert serialized.index("Inspect first source.") < serialized.index("Local reply.")
    assert serialized.index("Local reply.") < serialized.index("Inspect second source.")
    assert "Continue from the restored conversation" in json.dumps(requests[1])
