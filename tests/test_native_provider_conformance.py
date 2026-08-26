from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages
from alysis_code.compaction.conversation_compactor import ConversationCompactor
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.metadata import (
    PROVIDER_METADATA_KEY,
    assistant_message_from_response,
    strip_provider_metadata_from_message,
)
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import LLMError, LLMResponse
from alysis_code.request_estimation import sanitize_messages_for_estimation
from alysis_code.session_store import SessionStore, read_session_events


def _fs_read_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "fs_read",
            "description": "Read a file.",
            "parameters": {"type": "object"},
        },
    }


def _shell_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "shell_run",
            "description": "Run a command.",
            "parameters": {"type": "object"},
        },
    }


def _web_search_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Standalone Alysis Code web search.",
            "parameters": {"type": "object"},
        },
    }


def _assistant_message_from_response(response: LLMResponse) -> dict[str, Any]:
    return assistant_message_from_response(response)


@pytest.mark.parametrize(
    "provider", ["openai_responses", "anthropic_messages", "gemini_generate_content"]
)
def test_native_provider_conformance_multiturn_tool_replay_exact_ids(provider: str) -> None:
    calls: list[dict[str, Any]] = []

    if provider == "openai_responses":
        output_items = [
            {
                "type": "function_call",
                "id": "fc_read_item",
                "call_id": "call_read_exact",
                "name": "fs_read",
                "arguments": '{"path":"README.md"}',
                "status": "completed",
            },
            {
                "type": "function_call",
                "id": "fc_shell_item",
                "call_id": "call_shell_exact",
                "name": "shell_run",
                "arguments": '{"cmd":"pytest -q"}',
                "status": "completed",
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={"id": "resp_tools", "model": "gpt-5.5", "output": output_items},
                )
            return httpx.Response(
                200,
                json={"id": "resp_done", "output_text": "done", "output": []},
            )

        client = OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-5.5",
            transport=httpx.MockTransport(handler),
        )
        first = client.chat(
            messages=[{"role": "user", "content": "Inspect repo."}],
            tools=[_fs_read_tool(), _shell_tool()],
        )
        assistant_message = _assistant_message_from_response(first)
        client.chat(
            messages=[
                {"role": "user", "content": "Inspect repo."},
                assistant_message,
                {"role": "tool", "tool_call_id": "call_read_exact", "content": "README"},
                {"role": "tool", "tool_call_id": "call_shell_exact", "content": "tests passed"},
            ],
            tools=[_fs_read_tool(), _shell_tool()],
        )
        assert calls[1]["previous_response_id"] == "resp_tools"
        assert calls[1]["input"] == [
            {"type": "function_call_output", "call_id": "call_read_exact", "output": "README"},
            {
                "type": "function_call_output",
                "call_id": "call_shell_exact",
                "output": "tests passed",
            },
        ]
        return

    if provider == "anthropic_messages":
        content_blocks = [
            {
                "type": "tool_use",
                "id": "toolu_read_exact",
                "name": "fs_read",
                "input": {"path": "README.md"},
            },
            {
                "type": "tool_use",
                "id": "toolu_shell_exact",
                "name": "shell_run",
                "input": {"cmd": "pytest -q"},
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={"id": "msg_tools", "content": content_blocks, "stop_reason": "tool_use"},
                )
            return httpx.Response(
                200,
                json={"id": "msg_done", "content": [{"type": "text", "text": "done"}]},
            )

        client = AnthropicMessagesClient(
            base_url="https://api.anthropic.com/v1",
            api_key="test-key",
            model="claude-sonnet-4-6",
            transport=httpx.MockTransport(handler),
        )
        first = client.chat(
            messages=[{"role": "user", "content": "Inspect repo."}],
            tools=[_fs_read_tool(), _shell_tool()],
        )
        assistant_message = _assistant_message_from_response(first)
        client.chat(
            messages=[
                {"role": "user", "content": "Inspect repo."},
                assistant_message,
                {"role": "tool", "tool_call_id": "toolu_read_exact", "content": "README"},
                {"role": "tool", "tool_call_id": "toolu_shell_exact", "content": "tests passed"},
            ],
            tools=[_fs_read_tool(), _shell_tool()],
        )
        assert calls[1]["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Inspect repo."}]},
            {"role": "assistant", "content": content_blocks},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_read_exact", "content": "README"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_shell_exact",
                        "content": "tests passed",
                    },
                ],
            },
        ]
        return

    provider_content = {
        "role": "model",
        "parts": [
            {
                "functionCall": {
                    "id": "call_read_exact",
                    "name": "fs_read",
                    "args": {"path": "README.md"},
                },
                "thoughtSignature": "thought-read-exact",
            },
            {
                "functionCall": {
                    "id": "call_shell_exact",
                    "name": "shell_run",
                    "args": {"cmd": "pytest -q"},
                }
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={"responseId": "resp_tools", "candidates": [{"content": provider_content}]},
            )
        return httpx.Response(
            200,
            json={
                "responseId": "resp_done",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "Inspect repo."}],
        tools=[_fs_read_tool(), _shell_tool()],
    )
    assistant_message = _assistant_message_from_response(first)
    client.chat(
        messages=[
            {"role": "user", "content": "Inspect repo."},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_read_exact", "content": "README"},
            {"role": "tool", "tool_call_id": "call_shell_exact", "content": "tests passed"},
        ],
        tools=[_fs_read_tool(), _shell_tool()],
    )
    assert calls[1]["contents"] == [
        {"role": "user", "parts": [{"text": "Inspect repo."}]},
        provider_content,
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_read_exact",
                        "name": "fs_read",
                        "response": {"result": "README"},
                    }
                },
                {
                    "functionResponse": {
                        "id": "call_shell_exact",
                        "name": "shell_run",
                        "response": {"result": "tests passed"},
                    }
                },
            ],
        },
    ]


@pytest.mark.parametrize(
    "provider", ["openai_responses", "anthropic_messages", "gemini_generate_content"]
)
@pytest.mark.parametrize(
    ("mode", "adapter", "expect_hosted", "expect_external"),
    [
        ("off", "auto", False, False),
        ("native", "native", True, False),
        ("auto", "auto", True, False),
        ("auto", "tavily", False, True),
        ("external", "tavily", False, True),
    ],
)
def test_native_provider_conformance_web_search_policy_modes(
    provider: str,
    mode: str,
    adapter: str,
    expect_hosted: bool,
    expect_external: bool,
) -> None:
    native_adapter = {
        "openai_responses": "openai_responses",
        "anthropic_messages": "anthropic_messages",
        "gemini_generate_content": "gemini_grounding",
    }[provider]
    resolved_adapter = native_adapter if adapter == "native" else adapter
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        if provider == "openai_responses":
            return httpx.Response(200, json={"id": "resp_ok", "output_text": "ok", "output": []})
        if provider == "anthropic_messages":
            return httpx.Response(
                200, json={"id": "msg_ok", "content": [{"type": "text", "text": "ok"}]}
            )
        return httpx.Response(
            200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]}
        )

    if provider == "openai_responses":
        client = OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-5.5",
            web_search_mode=mode,
            web_search_adapter=resolved_adapter,
            transport=httpx.MockTransport(handler),
        )
    elif provider == "anthropic_messages":
        client = AnthropicMessagesClient(
            base_url="https://api.anthropic.com/v1",
            api_key="test-key",
            model="claude-sonnet-4-6",
            web_search_mode=mode,
            web_search_adapter=resolved_adapter,
            transport=httpx.MockTransport(handler),
        )
    else:
        client = GeminiGenerateContentClient(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="test-key",
            model="gemini-3-flash-preview",
            web_search_mode=mode,
            web_search_adapter=resolved_adapter,
            transport=httpx.MockTransport(handler),
        )

    client.chat(
        messages=[{"role": "user", "content": "Find docs."}],
        tools=[_web_search_tool(), _fs_read_tool()],
    )
    tools = captured.get("tools") or []
    if provider == "openai_responses":
        hosted = any(isinstance(tool, dict) and tool.get("type") == "web_search" for tool in tools)
        external = any(
            isinstance(tool, dict)
            and tool.get("type") == "function"
            and tool.get("name") == "web_search"
            for tool in tools
        )
    elif provider == "anthropic_messages":
        hosted = any(
            isinstance(tool, dict) and str(tool.get("type") or "").startswith("web_search_")
            for tool in tools
        )
        external = any(
            isinstance(tool, dict) and tool.get("name") == "web_search" and "type" not in tool
            for tool in tools
        )
    else:
        hosted = any(
            isinstance(tool, dict) and isinstance(tool.get("google_search"), dict) for tool in tools
        )
        external = any(
            isinstance(tool, dict)
            and any(
                declaration.get("name") == "web_search"
                for declaration in tool.get("functionDeclarations", [])
                if isinstance(declaration, dict)
            )
            for tool in tools
            if isinstance(tool, dict)
        )

    assert hosted is expect_hosted
    assert external is expect_external
    assert not (hosted and external)


@pytest.mark.parametrize(
    ("provider", "client"),
    [
        (
            "openai_responses",
            OpenAIResponsesClient(
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                model="gpt-5.5",
                web_search_mode="native",
                web_search_adapter="tavily",
                transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            ),
        ),
        (
            "anthropic_messages",
            AnthropicMessagesClient(
                base_url="https://api.anthropic.com/v1",
                api_key="test-key",
                model="claude-sonnet-4-6",
                web_search_mode="native",
                web_search_adapter="tavily",
                transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            ),
        ),
        (
            "gemini_generate_content",
            GeminiGenerateContentClient(
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key="test-key",
                model="gemini-3-flash-preview",
                web_search_mode="native",
                web_search_adapter="tavily",
                transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            ),
        ),
    ],
)
def test_native_provider_conformance_native_mode_rejects_external_adapter(
    provider: str,
    client: Any,
) -> None:
    with pytest.raises(LLMError, match=f"web_search_mode=native.*{provider.split('_')[0]}"):
        client.chat(
            messages=[{"role": "user", "content": "Find docs."}],
            tools=[_web_search_tool()],
        )


@pytest.mark.parametrize(
    ("provider_key", "metadata_payload"),
    [
        ("openai_responses", {"output_items": [{"type": "message", "id": "msg_1"}]}),
        ("anthropic_messages", {"content_blocks": [{"type": "text", "text": "hello"}]}),
        (
            "gemini_generate_content",
            {"content": {"role": "model", "parts": [{"text": "hello", "thoughtSignature": "sig"}]}},
        ),
    ],
)
def test_provider_metadata_survives_session_artifacts_and_is_stripped_for_public_surfaces(
    tmp_path: Path,
    provider_key: str,
    metadata_payload: dict[str, Any],
) -> None:
    message = {
        "role": "assistant",
        "content": "hello",
        PROVIDER_METADATA_KEY: {provider_key: metadata_payload},
    }
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id="session-1",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    store.append("assistant_message", {"message": message})
    store.close()

    events = list(read_session_events(store.path))
    assert events[0]["payload"]["message"][PROVIDER_METADATA_KEY][provider_key] == metadata_payload

    resumed = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id="session-1",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    try:
        resumed_events = resumed.events_snapshot()
    finally:
        resumed.close()
    assert (
        resumed_events[0]["payload"]["message"][PROVIDER_METADATA_KEY][provider_key]
        == metadata_payload
    )

    assert strip_provider_metadata_from_message(message) == {
        "role": "assistant",
        "content": "hello",
    }
    assert sanitize_messages_for_estimation([message]) == [
        {"role": "assistant", "content": "hello"}
    ]

    payload = ConversationCompactor._history_chunk_payload(idx=3, message=message)
    assert PROVIDER_METADATA_KEY not in payload["message"]
    assert payload["internal_message"][PROVIDER_METADATA_KEY][provider_key] == metadata_payload


def test_text_only_anthropic_search_metadata_survives_compaction_and_resume(
    tmp_path: Path,
) -> None:
    metadata_payload = {
        "message_id": "msg_text_search",
        "content_blocks": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_text",
                "name": "web_search",
                "input": {"query": "Alysis Code provider metadata"},
            },
            {
                "type": "text",
                "text": "Anthropic hosted search answered with citations.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://example.test/provider-metadata",
                        "title": "Provider metadata",
                        "encrypted_index": "encrypted-index",
                    }
                ],
            },
        ],
        "server_tool_uses": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_text",
                "name": "web_search",
            }
        ],
        "citations": [
            {
                "type": "web_search_result_location",
                "url": "https://example.test/provider-metadata",
                "title": "Provider metadata",
                "encrypted_index": "encrypted-index",
            }
        ],
    }
    assistant_message = {
        "role": "assistant",
        "content": "Anthropic hosted search answered with citations.",
        PROVIDER_METADATA_KEY: {"anthropic_messages": metadata_payload},
    }

    payload = ConversationCompactor._history_chunk_payload(idx=7, message=assistant_message)
    assert payload["message"] == {
        "role": "assistant",
        "content": "Anthropic hosted search answered with citations.",
    }
    assert (
        payload["internal_message"][PROVIDER_METADATA_KEY]["anthropic_messages"] == metadata_payload
    )

    session_log = tmp_path / "resume-text-only-native.jsonl"
    events = [
        {"type": "session_start", "payload": {"model": "native-model"}},
        {"type": "user_message", "payload": {"content": "Use hosted search"}},
        {
            "type": "assistant_message",
            "payload": {
                "content": "Anthropic hosted search answered with citations.",
                "message": assistant_message,
            },
        },
        {
            "type": "final",
            "payload": {"content": "Anthropic hosted search answered with citations."},
        },
    ]
    session_log.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    loaded = _load_chat_resume_messages(session_log)

    assert loaded == [
        {"role": "user", "content": "Use hosted search"},
        assistant_message,
    ]


@pytest.mark.parametrize(
    ("provider_key", "metadata_payload", "tool_call_id"),
    [
        (
            "openai_responses",
            {
                "output_items": [
                    {
                        "type": "function_call",
                        "id": "fc_resume",
                        "call_id": "call_resume_exact",
                        "name": "fs_read",
                        "arguments": '{"path":"README.md"}',
                    }
                ]
            },
            "call_resume_exact",
        ),
        (
            "anthropic_messages",
            {
                "content_blocks": [
                    {
                        "type": "tool_use",
                        "id": "toolu_resume_exact",
                        "name": "fs_read",
                        "input": {"path": "README.md"},
                    }
                ]
            },
            "toolu_resume_exact",
        ),
        (
            "gemini_generate_content",
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "call_resume_exact",
                                "name": "fs_read",
                                "args": {"path": "README.md"},
                            },
                            "thoughtSignature": "resume-thought",
                        }
                    ],
                }
            },
            "call_resume_exact",
        ),
    ],
)
def test_provider_metadata_survives_resume_history_reconstruction(
    tmp_path: Path,
    provider_key: str,
    metadata_payload: dict[str, Any],
    tool_call_id: str,
) -> None:
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "arguments": json.dumps({"path": "README.md"}),
                },
            }
        ],
        PROVIDER_METADATA_KEY: {provider_key: metadata_payload},
    }
    session_log = tmp_path / "resume-native.jsonl"
    events = [
        {"type": "session_start", "payload": {"model": "native-model"}},
        {"type": "user_message", "payload": {"content": "read README"}},
        {
            "type": "assistant_message",
            "payload": {
                "content": "",
                "tool_calls": ["fs_read"],
                "message": assistant_message,
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "fs_read",
                "tool_call_id": tool_call_id,
                "result": {"content": "README"},
                "step": 1,
            },
        },
    ]
    session_log.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    loaded = _load_chat_resume_messages(session_log)

    assert loaded == [
        {"role": "user", "content": "read README"},
        assistant_message,
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": '{"content":"README"}',
        },
    ]
