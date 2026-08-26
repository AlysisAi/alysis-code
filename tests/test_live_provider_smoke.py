from __future__ import annotations

import os

import pytest

from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.metadata import attach_provider_metadata_to_assistant_message
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.protocols import get_provider_protocol_capabilities
from alysis_code.llm.types import LLMResponse


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        pytest.skip(f"{name} is required for this live smoke test")
    return value


def _require_live_provider_smoke() -> None:
    if os.environ.get("ALYSIS_RUN_LIVE_PROVIDER_SMOKE") != "1":
        pytest.skip("set ALYSIS_RUN_LIVE_PROVIDER_SMOKE=1 to run live provider smoke tests")


def _require_live_provider_web_search_smoke() -> None:
    _require_live_provider_smoke()
    if os.environ.get("ALYSIS_RUN_LIVE_PROVIDER_WEB_SEARCH_SMOKE") != "1":
        pytest.skip("set ALYSIS_RUN_LIVE_PROVIDER_WEB_SEARCH_SMOKE=1 to run web search smoke")


def _require_live_provider_streaming_smoke(*, provider_key: str, protocol: str) -> None:
    _require_live_provider_smoke()
    if os.environ.get("ALYSIS_RUN_LIVE_PROVIDER_STREAMING_SMOKE") != "1":
        pytest.skip("set ALYSIS_RUN_LIVE_PROVIDER_STREAMING_SMOKE=1 to run streaming smoke")
    capabilities = get_provider_protocol_capabilities(
        provider_key=provider_key,
        protocol=protocol,
    )
    if capabilities is None or not capabilities.supports_streaming:
        pytest.skip(f"{protocol} does not support native streaming in this build")


def _tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "lookup_release_channel",
            "description": "Return the release channel for a product.",
            "parameters": {
                "type": "object",
                "properties": {"product": {"type": "string"}},
                "required": ["product"],
            },
        },
    }


def _forced_tool_choice() -> dict[str, object]:
    return {"type": "function", "function": {"name": "lookup_release_channel"}}


def _web_search_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current public information.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _provider_metadata(response: LLMResponse, key: str) -> dict[str, object]:
    metadata = response.provider_metadata or {}
    provider_metadata = metadata.get(key)
    assert isinstance(provider_metadata, dict), f"missing {key} provider metadata: {metadata!r}"
    return provider_metadata


def _nonempty(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return value is not None


def _nested_values_for_keys(value: object, names: set[str]) -> list[object]:
    values: list[object] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names:
                values.append(item)
            values.extend(_nested_values_for_keys(item, names))
        return values
    if isinstance(value, list):
        for item in value:
            values.extend(_nested_values_for_keys(item, names))
    return values


def _has_typed_block(value: object, block_types: set[str]) -> bool:
    if isinstance(value, dict):
        block_type = value.get("type")
        if isinstance(block_type, str) and block_type in block_types:
            return True
        return any(_has_typed_block(item, block_types) for item in value.values())
    if isinstance(value, list):
        return any(_has_typed_block(item, block_types) for item in value)
    return False


def _gemini_function_call_parts(metadata: dict[str, object]) -> list[dict[str, object]]:
    content = metadata.get("content")
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    if not isinstance(parts, list):
        return []
    return [
        part
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)
    ]


def test_live_provider_smoke_tests_are_gated_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_RUN_LIVE_PROVIDER_SMOKE", raising=False)
    monkeypatch.delenv("ALYSIS_RUN_LIVE_PROVIDER_WEB_SEARCH_SMOKE", raising=False)
    monkeypatch.delenv("ALYSIS_RUN_LIVE_PROVIDER_STREAMING_SMOKE", raising=False)

    with pytest.raises(pytest.skip.Exception):
        _require_live_provider_smoke()
    with pytest.raises(pytest.skip.Exception):
        _require_live_provider_web_search_smoke()
    with pytest.raises(pytest.skip.Exception):
        _require_live_provider_streaming_smoke(
            provider_key="anthropic",
            protocol="anthropic_messages",
        )


def test_live_native_web_search_smoke_requires_both_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_RUN_LIVE_PROVIDER_SMOKE", raising=False)
    monkeypatch.setenv("ALYSIS_RUN_LIVE_PROVIDER_WEB_SEARCH_SMOKE", "1")

    with pytest.raises(pytest.skip.Exception):
        _require_live_provider_web_search_smoke()

    monkeypatch.setenv("ALYSIS_RUN_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.delenv("ALYSIS_RUN_LIVE_PROVIDER_WEB_SEARCH_SMOKE", raising=False)
    with pytest.raises(pytest.skip.Exception):
        _require_live_provider_web_search_smoke()


def test_live_native_streaming_smoke_requires_both_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_RUN_LIVE_PROVIDER_SMOKE", raising=False)
    monkeypatch.setenv("ALYSIS_RUN_LIVE_PROVIDER_STREAMING_SMOKE", "1")

    with pytest.raises(pytest.skip.Exception):
        _require_live_provider_streaming_smoke(
            provider_key="gemini",
            protocol="gemini_generate_content",
        )

    monkeypatch.setenv("ALYSIS_RUN_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.delenv("ALYSIS_RUN_LIVE_PROVIDER_STREAMING_SMOKE", raising=False)
    with pytest.raises(pytest.skip.Exception):
        _require_live_provider_streaming_smoke(
            provider_key="gemini",
            protocol="gemini_generate_content",
        )


def test_live_openai_responses_text_smoke() -> None:
    _require_live_provider_smoke()
    client = OpenAIResponsesClient(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=_required_env("OPENAI_API_KEY"),
        model=os.environ.get("OPENAI_RESPONSES_SMOKE_MODEL", "gpt-5.4-mini"),
        timeout_s=15,
        web_search_mode="off",
    )

    response = client.chat(messages=[{"role": "user", "content": "Reply with only: ok"}])

    assert response.content.strip()


def test_live_openai_responses_previous_response_id_instruction_retention_smoke() -> None:
    _require_live_provider_smoke()
    client = OpenAIResponsesClient(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=_required_env("OPENAI_API_KEY"),
        model=os.environ.get("OPENAI_RESPONSES_SMOKE_MODEL", "gpt-5.4-mini"),
        timeout_s=15,
        web_search_mode="off",
    )
    first_prefix = [
        {"role": "system", "content": "Answer smoke-test prompts with short plain text."},
        {"role": "developer", "content": "Never reveal credentials or environment values."},
        {"role": "user", "content": "Reply with only: ok"},
    ]

    first = client.chat(messages=first_prefix)
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )
    second = client.chat(
        messages=[
            *first_prefix,
            assistant_message,
            {"role": "user", "content": "Reply with only: ok"},
        ],
    )

    assert second.content.strip()
    metadata = _provider_metadata(second, "openai_responses")
    request_plan = metadata.get("request_plan")
    assert isinstance(request_plan, dict)
    assert request_plan["input_mode"] == "previous_response_id"
    assert request_plan["previous_response_id_used"] is True
    assert request_plan["resent_stable_instruction_count"] == 0
    assert request_plan["sent_input_item_count"] == 1


def test_live_openai_responses_tool_call_smoke() -> None:
    _require_live_provider_smoke()
    client = OpenAIResponsesClient(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=_required_env("OPENAI_API_KEY"),
        model=os.environ.get("OPENAI_RESPONSES_SMOKE_MODEL", "gpt-5.4-mini"),
        timeout_s=15,
        web_search_mode="off",
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Use the tool for product Alysis Code."}],
        tools=[_tool()],
        tool_choice=_forced_tool_choice(),
    )

    assert response.tool_calls
    assert response.tool_calls[0].id


def test_live_anthropic_messages_text_smoke() -> None:
    _require_live_provider_smoke()
    client = AnthropicMessagesClient(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        api_key=_required_env("ANTHROPIC_API_KEY"),
        model=os.environ.get("ANTHROPIC_MESSAGES_SMOKE_MODEL", "claude-sonnet-4-6"),
        timeout_s=15,
        web_search_mode="off",
    )

    response = client.chat(messages=[{"role": "user", "content": "Reply with only: ok"}])

    assert response.content.strip()


def test_live_anthropic_messages_tool_call_smoke() -> None:
    _require_live_provider_smoke()
    client = AnthropicMessagesClient(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        api_key=_required_env("ANTHROPIC_API_KEY"),
        model=os.environ.get("ANTHROPIC_MESSAGES_SMOKE_MODEL", "claude-sonnet-4-6"),
        timeout_s=15,
        web_search_mode="off",
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Use the tool for product Alysis Code."}],
        tools=[_tool()],
        tool_choice=_forced_tool_choice(),
    )

    assert response.tool_calls
    assert response.tool_calls[0].id


def test_live_gemini_generate_content_text_smoke() -> None:
    _require_live_provider_smoke()
    client = GeminiGenerateContentClient(
        base_url=os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        api_key=_required_env("GEMINI_API_KEY"),
        model=os.environ.get("GEMINI_GENERATE_CONTENT_SMOKE_MODEL", "gemini-2.5-flash"),
        timeout_s=15,
        web_search_mode="off",
    )

    response = client.chat(messages=[{"role": "user", "content": "Reply with only: ok"}])

    assert response.content.strip()


def test_live_gemini_generate_content_tool_call_id_smoke() -> None:
    _require_live_provider_smoke()
    client = GeminiGenerateContentClient(
        base_url=os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        api_key=_required_env("GEMINI_API_KEY"),
        model=os.environ.get("GEMINI_GENERATE_CONTENT_SMOKE_MODEL", "gemini-2.5-flash"),
        timeout_s=15,
        web_search_mode="off",
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Use the tool for product Alysis Code."}],
        tools=[_tool()],
        tool_choice=_forced_tool_choice(),
    )

    assert response.tool_calls
    assert response.tool_calls[0].id


def test_live_native_web_search_smoke_guarded_separately() -> None:
    _require_live_provider_web_search_smoke()
    client = OpenAIResponsesClient(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=_required_env("OPENAI_API_KEY"),
        model=os.environ.get("OPENAI_RESPONSES_SMOKE_MODEL", "gpt-5.4-mini"),
        timeout_s=20,
        web_search_mode="native",
        web_search_adapter="openai_responses",
    )

    response = client.web_search(query="OpenAI homepage URL")

    assert response.answer.strip()
    assert response.sources
    assert response.response_id
    assert any(
        isinstance(item, dict) and item.get("type") == "web_search_call"
        for item in list(response.raw.get("output") or [])
    )


def test_live_anthropic_messages_native_web_search_smoke() -> None:
    _require_live_provider_web_search_smoke()
    client = AnthropicMessagesClient(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        api_key=_required_env("ANTHROPIC_API_KEY"),
        model=os.environ.get("ANTHROPIC_MESSAGES_SMOKE_MODEL", "claude-sonnet-4-6"),
        timeout_s=25,
        web_search_mode="native",
        web_search_adapter="anthropic_messages",
    )

    response = client.chat(
        messages=[
            {
                "role": "user",
                "content": (
                    "Use web search to find the official Anthropic documentation homepage. "
                    "Reply with one short sentence."
                ),
            }
        ],
        tools=[_web_search_tool()],
        max_tokens=160,
    )

    assert response.content.strip()
    metadata = _provider_metadata(response, "anthropic_messages")
    assert any(
        _nonempty(metadata.get(name))
        for name in ("server_tool_uses", "sources", "citations", "queries")
    ) or _has_typed_block(
        metadata.get("content_blocks"),
        {"server_tool_use", "web_search_tool_result"},
    )


def test_live_gemini_generate_content_native_web_search_smoke() -> None:
    _require_live_provider_web_search_smoke()
    client = GeminiGenerateContentClient(
        base_url=os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        api_key=_required_env("GEMINI_API_KEY"),
        model=os.environ.get("GEMINI_GENERATE_CONTENT_SMOKE_MODEL", "gemini-2.5-flash"),
        timeout_s=25,
        web_search_mode="native",
        web_search_adapter="gemini_grounding",
    )

    response = client.chat(
        messages=[
            {
                "role": "user",
                "content": (
                    "Use Google Search grounding to find the official Gemini API documentation. "
                    "Reply with one short sentence."
                ),
            }
        ],
        tools=[_web_search_tool()],
        max_tokens=160,
    )

    assert response.content.strip()
    metadata = _provider_metadata(response, "gemini_generate_content")
    assert any(
        _nonempty(metadata.get(name))
        for name in ("groundingMetadata", "queries", "sources", "citations")
    ) or any(
        _nonempty(value)
        for value in _nested_values_for_keys(
            metadata,
            {"webSearchQueries", "groundingChunks", "groundingSupports"},
        )
    )


def test_live_anthropic_messages_text_streaming_smoke() -> None:
    _require_live_provider_streaming_smoke(
        provider_key="anthropic",
        protocol="anthropic_messages",
    )
    deltas: list[str] = []
    client = AnthropicMessagesClient(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        api_key=_required_env("ANTHROPIC_API_KEY"),
        model=os.environ.get("ANTHROPIC_MESSAGES_SMOKE_MODEL", "claude-sonnet-4-6"),
        timeout_s=20,
        web_search_mode="off",
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Reply with only: ok"}],
        stream=True,
        on_text_delta=deltas.append,
        max_tokens=20,
    )

    assert response.content.strip()
    assert "".join(deltas).strip() == response.content.strip()


def test_live_anthropic_messages_tool_use_streaming_smoke() -> None:
    _require_live_provider_streaming_smoke(
        provider_key="anthropic",
        protocol="anthropic_messages",
    )
    client = AnthropicMessagesClient(
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
        api_key=_required_env("ANTHROPIC_API_KEY"),
        model=os.environ.get("ANTHROPIC_MESSAGES_SMOKE_MODEL", "claude-sonnet-4-6"),
        timeout_s=20,
        web_search_mode="off",
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Use the tool for product Alysis Code."}],
        tools=[_tool()],
        tool_choice=_forced_tool_choice(),
        stream=True,
        max_tokens=120,
    )

    assert response.tool_calls
    assert response.tool_calls[0].id


def test_live_gemini_generate_content_text_streaming_smoke() -> None:
    _require_live_provider_streaming_smoke(
        provider_key="gemini",
        protocol="gemini_generate_content",
    )
    deltas: list[str] = []
    client = GeminiGenerateContentClient(
        base_url=os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        api_key=_required_env("GEMINI_API_KEY"),
        model=os.environ.get("GEMINI_GENERATE_CONTENT_SMOKE_MODEL", "gemini-2.5-flash"),
        timeout_s=20,
        web_search_mode="off",
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Reply with only: ok"}],
        stream=True,
        on_text_delta=deltas.append,
        max_tokens=20,
    )

    assert response.content.strip()
    assert "".join(deltas).strip() == response.content.strip()


def test_live_gemini_generate_content_function_call_streaming_smoke() -> None:
    _require_live_provider_streaming_smoke(
        provider_key="gemini",
        protocol="gemini_generate_content",
    )
    client = GeminiGenerateContentClient(
        base_url=os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        api_key=_required_env("GEMINI_API_KEY"),
        model=os.environ.get("GEMINI_GENERATE_CONTENT_SMOKE_MODEL", "gemini-2.5-flash"),
        timeout_s=20,
        web_search_mode="off",
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Use the tool for product Alysis Code."}],
        tools=[_tool()],
        tool_choice=_forced_tool_choice(),
        stream=True,
        max_tokens=120,
    )

    assert response.tool_calls
    tool_call = response.tool_calls[0]
    assert tool_call.id
    metadata = _provider_metadata(response, "gemini_generate_content")
    function_parts = _gemini_function_call_parts(metadata)
    matching_parts = [
        part
        for part in function_parts
        if isinstance(part.get("functionCall"), dict)
        and part["functionCall"].get("id") == tool_call.id  # type: ignore[index]
    ]
    assert matching_parts, f"missing streamed Gemini functionCall part for {tool_call.id!r}"

    tool_metadata = (tool_call.provider_metadata or {}).get("gemini_generate_content")
    assert isinstance(tool_metadata, dict)
    assert "part_index" in tool_metadata

    thought_signature = matching_parts[0].get("thoughtSignature") or matching_parts[0].get(
        "thought_signature"
    )
    if thought_signature:
        assert thought_signature in {
            tool_metadata.get("thoughtSignature"),
            tool_metadata.get("thought_signature"),
        }
