from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.metadata import (
    PROVIDER_METADATA_KEY,
    attach_provider_metadata_to_assistant_message,
    stamp_provider_metadata_for_route,
    strip_provider_metadata_from_message,
)
from alysis_code.llm.openai_responses import (
    OpenAIResponsesClient,
    ResponsesError,
)
from alysis_code.llm.protocols import (
    OPENAI_RESPONSES_PROTOCOL,
    resolve_reasoning_trace_capability,
)
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError, ReasoningOutputKind
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)


def _client(transport: httpx.BaseTransport) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="search-model",
        transport=transport,
    )


def _with_openai_reasoning_summary(
    client: OpenAIResponsesClient,
) -> OpenAIResponsesClient:
    client.reasoning_trace_capability = resolve_reasoning_trace_capability(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model_supports_reasoning=True,
    )
    return client


def _sse_event(event_type: str, data: dict[str, object]) -> bytes:
    return (f"event: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n").encode()


def _sse_response(events: list[tuple[str, dict[str, object]]]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"".join(_sse_event(event, data) for event, data in events),
    )


class _TruncatedResponsesSummaryStream(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        yield _sse_event(
            "response.reasoning_summary_text.delta",
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "rs_1",
                "output_index": 0,
                "summary_index": 0,
                "delta": "Safe partial summary.",
            },
        )
        raise httpx.RemoteProtocolError("stream closed after partial summary")


def test_stream_does_not_retry_after_public_reasoning_summary() -> None:
    attempts = 0
    summaries: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_TruncatedResponsesSummaryStream(),
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="search-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _seconds: None,
    )

    with pytest.raises(LLMError):
        client.chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
            on_reasoning_delta=summaries.append,
        )

    assert attempts == 1
    assert summaries == ["Safe partial summary."]


def test_count_input_tokens_uses_provider_endpoint_with_full_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses/input_tokens"
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"object": "response.input_tokens", "input_tokens": 321},
        )

    result = _client(httpx.MockTransport(handler)).count_input_tokens(
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Read README.md"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert result is not None
    assert result.input_tokens == 321
    assert result.source.value == "provider_count"
    assert result.confidence.value == "authoritative"
    assert captured["model"] == "search-model"
    assert isinstance(captured.get("input"), list)
    assert isinstance(captured.get("tools"), list)


def test_count_input_tokens_caches_unsupported_endpoint() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = _client(httpx.MockTransport(handler))
    kwargs = {"messages": [{"role": "user", "content": "hello"}]}

    assert client.count_input_tokens(**kwargs) is None
    assert client.count_input_tokens(**kwargs) is None
    assert calls == 1


def test_count_input_tokens_uses_shared_provider_retry_policy() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"input_tokens": 77})

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="search-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=1,
            base_delay_seconds=0.01,
            max_delay_seconds=0.01,
        ),
        provider_sleep_fn=sleeps.append,
        provider_random_fn=lambda: 0.5,
    )

    result = client.count_input_tokens(messages=[{"role": "user", "content": "hello"}])

    assert result is not None
    assert result.input_tokens == 77
    assert calls == 2
    assert sleeps == [pytest.approx(0.01)]


def test_subscription_count_input_tokens_uses_adapter_without_generation_fields() -> None:
    class SubscriptionAuth:
        requires_streaming = True
        supports_previous_response_id = False

        def authorization_headers(
            self,
            _url: str,
            *,
            force_refresh: bool = False,
            session_id: str | None = None,
        ) -> dict[str, str]:
            _ = force_refresh, session_id
            return {"Authorization": "Bearer subscription-token"}

        def adapt_responses_payload(self, payload: dict[str, object]) -> dict[str, object]:
            adapted = dict(payload)
            adapted["instructions"] = "subscription instructions"
            adapted["store"] = False
            adapted["include"] = ["reasoning.encrypted_content"]
            adapted["text"] = {"verbosity": "low"}
            return adapted

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer subscription-token"
        body = json.loads(request.content.decode("utf-8"))
        assert body["instructions"] == "subscription instructions"
        assert "store" not in body
        assert "include" not in body
        assert "text" not in body
        return httpx.Response(200, json={"input_tokens": 42})

    client = OpenAIResponsesClient(
        base_url="https://chatgpt.example/backend-api/codex",
        api_key="",
        model="subscription-model",
        provider_auth=SubscriptionAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )

    result = client.count_input_tokens(messages=[{"role": "user", "content": "hello"}])

    assert result is not None
    assert result.input_tokens == 42


def test_provider_can_require_streaming_for_buffered_internal_calls() -> None:
    class StreamingRequiredAuth:
        requires_streaming = True
        supports_previous_response_id = False

        def authorization_headers(
            self,
            _url: str,
            *,
            force_refresh: bool = False,
            session_id: str | None = None,
        ) -> dict[str, str]:
            return {"Authorization": "Bearer subscription"}

        def adapt_responses_payload(self, payload: dict[str, object]) -> dict[str, object]:
            return dict(payload)

    text_deltas: list[str] = []
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["stream"] is True
        assert body["reasoning"] == {"summary": "auto"}
        return _sse_response(
            [
                (
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_buffered",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "Safe buffered ",
                    },
                ),
                (
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_buffered",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "summary.",
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_buffered",
                        "output_index": 1,
                        "content_index": 0,
                        "delta": "streamed ",
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_buffered",
                        "output_index": 1,
                        "content_index": 0,
                        "delta": "internally",
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_required_stream",
                            "model": "subscription-model",
                            "status": "completed",
                            "output_text": "streamed internally",
                            "output": [
                                {
                                    "type": "reasoning",
                                    "id": "rs_buffered",
                                    "summary": [
                                        {
                                            "type": "summary_text",
                                            "text": "Safe buffered summary.",
                                        }
                                    ],
                                },
                                {
                                    "type": "message",
                                    "id": "msg_buffered",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": "streamed internally",
                                        }
                                    ],
                                },
                            ],
                            "usage": {
                                "input_tokens": 2,
                                "output_tokens": 2,
                                "total_tokens": 4,
                            },
                        },
                    },
                ),
            ]
        )

    client = _with_openai_reasoning_summary(
        OpenAIResponsesClient(
            base_url="https://subscription.example/v1",
            api_key="",
            model="subscription-model",
            provider_auth=StreamingRequiredAuth(),  # type: ignore[arg-type]
            transport=httpx.MockTransport(handler),
        )
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
        on_text_delta=text_deltas.append,
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert response.content == "streamed internally"
    # The subscription transport uses SSE internally, but buffered mode exposes
    # one normalized callback per final value rather than live transport chunks.
    assert reasoning_deltas == ["Safe buffered summary."]
    assert text_deltas == ["streamed internally"]


def test_chat_maps_messages_tools_response_format_and_reasoning() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "resp_chat_1",
                "model": "gpt-5.5",
                "output_text": "Done.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done."}],
                    }
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 3,
                    "total_tokens": 14,
                    "input_tokens_details": {"cached_tokens": 5},
                },
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        temperature=0.4,
        reasoning_effort="low",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "system", "content": "System prompt."},
            {"role": "developer", "content": "Developer prompt."},
            {"role": "user", "content": "Read it."},
            {
                "role": "assistant",
                "content": "I will read.",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {
                            "name": "fs_read",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_read", "content": "README contents"},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    {"type": "text", "text": "Describe the image."},
                ],
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "fs_read"}},
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "result",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                "strict": True,
            },
        },
        max_tokens=123,
    )

    assert captured["model"] == "gpt-5.5"
    assert captured["temperature"] == 0.4
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["max_output_tokens"] == 123
    assert captured["input"] == [
        {"role": "system", "content": "System prompt."},
        {"role": "developer", "content": "Developer prompt."},
        {"role": "user", "content": "Read it."},
        {"role": "assistant", "content": "I will read."},
        {
            "type": "function_call",
            "call_id": "call_read",
            "name": "fs_read",
            "arguments": '{"path":"README.md"}',
        },
        {"type": "function_call_output", "call_id": "call_read", "output": "README contents"},
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                {"type": "input_text", "text": "Describe the image."},
            ],
        },
    ]
    assert captured["tools"] == [
        {
            "type": "function",
            "name": "fs_read",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    assert captured["tool_choice"] == {"type": "function", "name": "fs_read"}
    assert captured["text"] == {
        "format": {
            "type": "json_schema",
            "name": "result",
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            "strict": True,
        }
    }
    assert response.content == "Done."
    assert response.usage is not None
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 3
    assert response.usage.total_tokens == 14
    assert response.usage.cached_prompt_tokens == 5
    assert response.usage.cache_read_input_tokens == 5
    assert response.usage.input_tokens_uncached == 6


def test_chat_clamps_max_output_tokens_to_responses_minimum(caplog) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "resp_minimum",
                "model": "search-model",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
            },
        )

    caplog.set_level("WARNING", logger="alysis_code.llm.openai_responses")
    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=1,
    )

    assert response.content == "ok"
    assert captured["max_output_tokens"] == 16
    assert "max_output_tokens adjusted from 1 to documented minimum 16" in caplog.text


def test_chat_normalizes_provider_declared_final_answer_phase() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_final_phase",
                "model": "gpt-5.5",
                "output_text": "The Next.js site is ready.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "The Next.js site is ready."}],
                    }
                ],
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "Check the site."}]
    )

    assert response.assistant_phase == "final_answer"


def test_chat_requests_reasoning_summary_without_overriding_explicit_effort() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "resp_reasoning_summary",
                "model": "gpt-5.5",
                "output_text": "Done.",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Checked the constraints."}],
                    }
                ],
            },
        )

    client = _with_openai_reasoning_summary(
        OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-5.5",
            reasoning_effort="high",
            transport=httpx.MockTransport(handler),
        )
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Think carefully."}],
    )

    assert response.content == "Done."
    assert captured["reasoning"] == {"effort": "high", "summary": "auto"}
    assert [item.kind for item in response.reasoning] == [ReasoningOutputKind.SUMMARY]
    assert [item.text for item in response.reasoning] == ["Checked the constraints."]


def test_chat_rejected_reasoning_summary_is_scoped_to_one_model() -> None:
    requests: list[dict[str, object]] = []
    summaries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported parameter: 'reasoning.summary'.",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "model": "gpt-5.5",
                "output_text": "Done.",
                "output": [],
            },
        )

    client = _with_openai_reasoning_summary(
        OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-5.5",
            reasoning_effort="high",
            transport=httpx.MockTransport(handler),
        )
    )

    first = client.chat(
        messages=[{"role": "user", "content": "Think carefully."}],
        on_reasoning_delta=summaries.append,
    )
    second = client.chat(
        messages=[{"role": "user", "content": "Try again."}],
        on_reasoning_delta=summaries.append,
    )
    client.model = "gpt-5.6"
    third = client.chat(
        messages=[{"role": "user", "content": "Use the other model."}],
        on_reasoning_delta=summaries.append,
    )

    assert first.content == "Done."
    assert second.content == "Done."
    assert third.content == "Done."
    assert requests[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert requests[1]["reasoning"] == {"effort": "high"}
    assert requests[2]["reasoning"] == {"effort": "high"}
    assert requests[3]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert summaries == []


def test_chat_streaming_retries_once_without_rejected_reasoning_summary() -> None:
    requests: list[dict[str, object]] = []
    summaries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                422,
                json={
                    "error": {
                        "message": "Unknown field reasoning.summary for this model.",
                    }
                },
            )
        return _sse_response(
            [
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_summary_fallback_stream",
                            "model": "gpt-5.5",
                            "status": "completed",
                            "output_text": "Done.",
                            "output": [],
                        },
                    },
                )
            ]
        )

    client = _with_openai_reasoning_summary(
        OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-5.5",
            reasoning_effort="medium",
            transport=httpx.MockTransport(handler),
        )
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Think carefully."}],
        stream=True,
        on_reasoning_delta=summaries.append,
    )

    assert response.content == "Done."
    assert requests[0]["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert requests[1]["reasoning"] == {"effort": "medium"}
    assert summaries == []


def test_chat_includes_prompt_cache_fields_and_records_safe_cache_policy() -> None:
    reset_provider_telemetry_for_tests()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "resp_cache",
                "model": "gpt-5.5",
                "output_text": "Done.",
                "output": [],
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(messages=[{"role": "user", "content": "cache me"}])

    assert response.content == "Done."
    assert "reasoning" not in captured
    assert captured["prompt_cache_key"] == "repo-main"
    assert captured["prompt_cache_retention"] == "24h"
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"] == {
        "status": "enabled",
        "strategy": "openai_prompt_cache",
        "mode": "automatic",
        "retention": "24h",
        "enabled": True,
    }
    assert summary["token_reconciliation"]["input_estimate_tokens"] > 0
    assert summary["token_reconciliation"]["sent_input_estimate_tokens"] > 0
    assert summary["token_reconciliation"]["reported_prompt_tokens"] is None
    assert "repo-main" not in json.dumps(summary, sort_keys=True)


def test_chat_parses_function_calls_and_usage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_tool",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_shell",
                        "name": "shell_run",
                        "arguments": '{"cmd":"pytest -q"}',
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "Run tests."}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "shell_run",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert response.content == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_shell"
    assert response.tool_calls[0].name == "shell_run"
    assert response.tool_calls[0].arguments == {"cmd": "pytest -q"}
    assert response.usage is not None
    assert response.usage.prompt_tokens == 8
    assert response.provider_metadata is not None
    assert response.provider_metadata["openai_responses"]["response_id"] == "resp_tool"
    assert response.provider_metadata["openai_responses"]["output_items"][0]["id"] == "fc_1"


def _web_search_function_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Standalone Alysis Code web search.",
            "parameters": {"type": "object"},
        },
    }


def test_chat_native_mode_uses_only_openai_hosted_web_search() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "resp_search_chat",
                "model": "gpt-5.5",
                "output_text": "Use the cited docs.",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_1",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "query": "Alysis Code docs",
                            "sources": [
                                {"title": "Docs", "url": "https://docs.example.com/alysis"}
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Use the cited docs.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "Docs",
                                        "url": "https://docs.example.com/alysis",
                                        "start_index": 8,
                                        "end_index": 18,
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="native",
        web_search_adapter="openai_responses",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Find current docs."}],
        tools=[_web_search_function_tool()],
    )

    assert captured["tools"] == [
        {"type": "web_search", "external_web_access": True},
    ]
    assert captured["include"] == ["web_search_call.action.sources"]
    assert response.content == "Use the cited docs."
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["openai_responses"]
    assert metadata["citations"][0]["url"] == "https://docs.example.com/alysis"
    assert metadata["sources"][0]["url"] == "https://docs.example.com/alysis"
    assert metadata["queries"] == ["Alysis Code docs"]


def test_chat_auto_mode_prefers_openai_hosted_web_search_for_auto_adapter() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={"id": "resp_no_search", "output_text": "ok", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="auto",
        web_search_adapter="auto",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[_web_search_function_tool()],
    )

    assert captured["tools"] == [{"type": "web_search", "external_web_access": True}]
    assert captured["include"] == ["web_search_call.action.sources"]
    assert response.content == "ok"


def test_chat_external_mode_uses_only_alysis_web_search_function() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={"id": "resp_external_search", "output_text": "ok", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="external",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[_web_search_function_tool()],
    )

    assert captured["tools"] == [
        {
            "type": "function",
            "name": "web_search",
            "description": "Standalone Alysis Code web search.",
            "parameters": {"type": "object"},
        }
    ]
    assert "include" not in captured
    assert response.content == "ok"


def test_chat_off_mode_removes_all_web_search_tools() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={"id": "resp_no_search", "output_text": "ok", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="off",
        web_search_adapter="openai_responses",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            _web_search_function_tool(),
            {"type": "web_search", "external_web_access": True},
        ],
    )

    assert "tools" not in captured
    assert "include" not in captured
    assert response.content == "ok"


def test_chat_auto_mode_with_external_adapter_uses_alysis_web_search_function() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={"id": "resp_auto_external", "output_text": "ok", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="auto",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[_web_search_function_tool()],
    )

    assert captured["tools"] == [
        {
            "type": "function",
            "name": "web_search",
            "description": "Standalone Alysis Code web search.",
            "parameters": {"type": "object"},
        }
    ]
    assert "include" not in captured
    assert response.content == "ok"


def test_chat_native_mode_rejects_non_openai_search_adapter() -> None:
    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="native",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(LLMError, match="web_search_mode=native.*openai_responses"):
        client.chat(
            messages=[{"role": "user", "content": "hello"}],
            tools=[_web_search_function_tool()],
        )


def test_chat_rejects_forced_removed_web_search_tool_choice() -> None:
    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="native",
        web_search_adapter="openai_responses",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(LLMError, match="removed the Alysis Code web_search function"):
        client.chat(
            messages=[{"role": "user", "content": "hello"}],
            tools=[_web_search_function_tool()],
            tool_choice={"type": "function", "function": {"name": "web_search"}},
        )


def test_chat_surfaces_provider_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad responses request"}})

    with pytest.raises(LLMError, match="LLM error 400: bad responses request"):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )


def test_chat_rejects_refusal_or_empty_output() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_refusal",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "text": "I cannot help with that."}],
                    }
                ],
            },
        )

    with pytest.raises(LLMError, match="OpenAI Responses refusal: I cannot help"):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )


def test_chat_streaming_emits_text_deltas_and_preserves_metadata() -> None:
    captured: dict[str, object] = {}
    deltas: list[str] = []
    message_item = {
        "type": "message",
        "id": "msg_stream",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "Hello stream."}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return _sse_response(
            [
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {
                            "id": "resp_stream",
                            "model": "gpt-5.5",
                            "status": "in_progress",
                            "output": [],
                        },
                    },
                ),
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "response_id": "resp_stream",
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "id": "msg_stream",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "response_id": "resp_stream",
                        "item_id": "msg_stream",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "Hello ",
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "response_id": "resp_stream",
                        "item_id": "msg_stream",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "stream.",
                    },
                ),
                (
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "response_id": "resp_stream",
                        "item_id": "msg_stream",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "Hello stream.",
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_stream",
                            "model": "gpt-5.5",
                            "status": "completed",
                            "output_text": "Hello stream.",
                            "output": [message_item],
                            "usage": {
                                "input_tokens": 5,
                                "output_tokens": 3,
                                "total_tokens": 8,
                            },
                        },
                    },
                ),
            ]
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        on_text_delta=deltas.append,
    )

    assert captured["stream"] is True
    assert response.content == "Hello stream."
    assert response.assistant_phase == "final_answer"
    assert deltas == ["Hello ", "stream."]
    assert response.usage is not None
    assert response.usage.total_tokens == 8
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["openai_responses"]
    assert metadata["response_id"] == "resp_stream"
    assert metadata["output_items"] == [message_item]
    assert metadata["stream_metadata"] == {"events": 6}


def test_chat_streaming_emits_official_reasoning_summary_events() -> None:
    captured: dict[str, object] = {}
    reasoning: list[str] = []
    text: list[str] = []
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_stream",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "I should check."}],
    }
    message_item = {
        "type": "message",
        "id": "msg_stream",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Checked."}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return _sse_response(
            [
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "reasoning",
                            "id": "rs_stream",
                            "status": "in_progress",
                            "summary": [],
                        },
                    },
                ),
                (
                    "response.reasoning_summary_part.added",
                    {
                        "type": "response.reasoning_summary_part.added",
                        "item_id": "rs_stream",
                        "output_index": 0,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    },
                ),
                (
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_stream",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "I should ",
                    },
                ),
                (
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_stream",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "check.",
                    },
                ),
                (
                    "response.reasoning_summary_text.done",
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": "rs_stream",
                        "output_index": 0,
                        "summary_index": 0,
                        "text": "I should check.",
                    },
                ),
                (
                    "response.reasoning_summary_part.done",
                    {
                        "type": "response.reasoning_summary_part.done",
                        "item_id": "rs_stream",
                        "output_index": 0,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": "I should check."},
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": reasoning_item,
                    },
                ),
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": 1,
                        "item": {
                            "type": "message",
                            "id": "msg_stream",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_stream",
                        "output_index": 1,
                        "content_index": 0,
                        "delta": "Checked.",
                    },
                ),
                (
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": "msg_stream",
                        "output_index": 1,
                        "content_index": 0,
                        "text": "Checked.",
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_reasoning_stream",
                            "status": "completed",
                            "output_text": "Checked.",
                            "output": [reasoning_item, message_item],
                        },
                    },
                ),
            ]
        )

    response = _with_openai_reasoning_summary(_client(httpx.MockTransport(handler))).chat(
        messages=[{"role": "user", "content": "check"}],
        stream=True,
        on_text_delta=text.append,
        on_reasoning_delta=reasoning.append,
    )

    assert response.content == "Checked."
    assert captured["reasoning"] == {"summary": "auto"}
    assert reasoning == ["I should ", "check."]
    assert text == ["Checked."]
    assert [item.text for item in response.reasoning] == ["I should check."]
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["openai_responses"]
    assert metadata["output_items"] == [reasoning_item, message_item]
    assert metadata["stream_metadata"] == {"events": 11}


def test_chat_streaming_supports_unindexed_compat_reasoning_delta() -> None:
    reasoning: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                (
                    "response.reasoning_summary.delta",
                    {
                        "type": "response.reasoning_summary.delta",
                        "delta": "Provider summary",
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_compat_summary",
                            "status": "completed",
                            "output_text": "Done.",
                            "output": [],
                        },
                    },
                ),
            ]
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "check"}],
        stream=True,
        on_reasoning_delta=reasoning.append,
    )

    assert response.content == "Done."
    assert reasoning == ["Provider summary"]
    assert response.provider_metadata is not None
    assert response.provider_metadata["openai_responses"]["stream_metadata"] == {"events": 2}


def test_chat_streaming_surfaces_done_only_reasoning_summary_once() -> None:
    reasoning: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                (
                    "response.reasoning_summary_text.done",
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": "rs_done_only",
                        "output_index": 0,
                        "summary_index": 0,
                        "text": "Completed provider summary.",
                    },
                ),
                (
                    "response.reasoning_summary_part.done",
                    {
                        "type": "response.reasoning_summary_part.done",
                        "item_id": "rs_done_only",
                        "output_index": 0,
                        "summary_index": 0,
                        "part": {
                            "type": "summary_text",
                            "text": "Completed provider summary.",
                        },
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_done_only_summary",
                            "status": "completed",
                            "output_text": "Done.",
                            "output": [],
                        },
                    },
                ),
            ]
        )

    _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "check"}],
        stream=True,
        on_reasoning_delta=reasoning.append,
    )

    assert reasoning == ["Completed provider summary."]


def test_chat_streaming_parses_function_call_argument_deltas() -> None:
    function_item = {
        "type": "function_call",
        "id": "fc_stream",
        "call_id": "call_stream",
        "name": "fs_read",
        "arguments": '{"path":"README.md"}',
        "status": "completed",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "response_id": "resp_tool_stream",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_stream",
                            "call_id": "call_stream",
                            "name": "fs_read",
                            "arguments": "",
                        },
                    },
                ),
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "response_id": "resp_tool_stream",
                        "item_id": "fc_stream",
                        "output_index": 0,
                        "delta": '{"path":',
                    },
                ),
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "response_id": "resp_tool_stream",
                        "item_id": "fc_stream",
                        "output_index": 0,
                        "delta": '"README.md"}',
                    },
                ),
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "response_id": "resp_tool_stream",
                        "item_id": "fc_stream",
                        "output_index": 0,
                        "call_id": "call_stream",
                        "name": "fs_read",
                        "arguments": '{"path":"README.md"}',
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "response_id": "resp_tool_stream",
                        "output_index": 0,
                        "item": function_item,
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_tool_stream",
                            "model": "gpt-5.5",
                            "status": "completed",
                            "output": [function_item],
                        },
                    },
                ),
            ]
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "fs_read", "parameters": {"type": "object"}},
            }
        ],
        stream=True,
    )

    assert response.content == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_stream"
    assert response.tool_calls[0].name == "fs_read"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.provider_metadata is not None
    assert response.provider_metadata["openai_responses"]["output_items"] == [function_item]


def test_chat_streaming_preserves_hosted_web_search_citations_and_sources() -> None:
    web_search_item = {
        "type": "web_search_call",
        "id": "ws_stream",
        "status": "completed",
        "action": {
            "type": "search",
            "query": "current docs",
            "sources": [{"title": "Docs", "url": "https://docs.example.com/current"}],
        },
    }
    message_item = {
        "type": "message",
        "id": "msg_search_stream",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": "Use current docs.",
                "annotations": [
                    {
                        "type": "url_citation",
                        "title": "Docs",
                        "url": "https://docs.example.com/current",
                        "start_index": 4,
                        "end_index": 16,
                    }
                ],
            }
        ],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "response_id": "resp_search_stream",
                        "output_index": 0,
                        "item": {
                            "type": "web_search_call",
                            "id": "ws_stream",
                            "status": "in_progress",
                        },
                    },
                ),
                (
                    "response.web_search_call.searching",
                    {
                        "type": "response.web_search_call.searching",
                        "response_id": "resp_search_stream",
                        "item_id": "ws_stream",
                        "output_index": 0,
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "response_id": "resp_search_stream",
                        "output_index": 0,
                        "item": web_search_item,
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "response_id": "resp_search_stream",
                        "item_id": "msg_search_stream",
                        "output_index": 1,
                        "content_index": 0,
                        "delta": "Use current docs.",
                    },
                ),
                (
                    "response.output_text.annotation.added",
                    {
                        "type": "response.output_text.annotation.added",
                        "response_id": "resp_search_stream",
                        "item_id": "msg_search_stream",
                        "output_index": 1,
                        "content_index": 0,
                        "annotation_index": 0,
                        "annotation": message_item["content"][0]["annotations"][0],
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_search_stream",
                            "model": "gpt-5.5",
                            "status": "completed",
                            "output_text": "Use current docs.",
                            "output": [web_search_item, message_item],
                        },
                    },
                ),
            ]
        )

    response = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        web_search_mode="native",
        web_search_adapter="openai_responses",
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "search"}],
        tools=[_web_search_function_tool()],
        stream=True,
    )

    assert response.content == "Use current docs."
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["openai_responses"]
    assert metadata["web_search_calls"] == [web_search_item]
    assert metadata["citations"][0]["url"] == "https://docs.example.com/current"
    assert metadata["sources"][0]["url"] == "https://docs.example.com/current"
    assert metadata["queries"] == ["current docs"]


def test_chat_streaming_error_event_is_clear() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                (
                    "error",
                    {
                        "type": "error",
                        "error": {"message": "stream exploded"},
                    },
                )
            ]
        )

    with pytest.raises(LLMError, match="OpenAI Responses stream error: stream exploded"):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )


def test_chat_streaming_malformed_event_is_clear() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"event: response.output_text.delta\ndata: {bad-json}\n\n",
        )

    with pytest.raises(LLMError, match="OpenAI Responses stream emitted malformed JSON"):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )


def test_chat_streaming_early_termination_is_clear() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "response_id": "resp_short",
                        "item_id": "msg_short",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "partial",
                    },
                )
            ]
        )

    with pytest.raises(LLMError, match="ended before response.completed"):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )


def test_chat_reasoning_only_stream_without_completed_routes_to_recovery() -> None:
    attempts = 0
    summaries: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _sse_response(
            [
                (
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_partial",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "Provider summary.",
                    },
                ),
                (
                    "response.reasoning_summary_text.done",
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": "rs_partial",
                        "output_index": 0,
                        "summary_index": 0,
                        "text": "Provider summary.",
                    },
                ),
            ]
        )

    client = _with_openai_reasoning_summary(
        OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-5.5",
            transport=httpx.MockTransport(handler),
            provider_retry_settings=ProviderRetrySettings(max_retries=5),
            provider_sleep_fn=lambda _seconds: None,
        )
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        on_reasoning_delta=summaries.append,
    )

    assert attempts == 1
    assert response.content == ""
    assert response.tool_calls == []
    assert [item.text for item in response.reasoning] == ["Provider summary."]
    assert summaries == ["Provider summary."]
    assert response.provider_metadata is not None
    stream_metadata = response.provider_metadata["openai_responses"]["stream_metadata"]
    assert stream_metadata["ended_before_response_completed"] is True


def test_chat_text_only_response_preserves_provider_metadata_for_next_turn() -> None:
    reset_provider_telemetry_for_tests()
    calls: list[dict[str, object]] = []
    output_items = [
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "First answer."}],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_text",
                    "model": "gpt-5.5",
                    "output_text": "First answer.",
                    "output": output_items,
                },
            )
        return httpx.Response(
            200,
            json={"id": "resp_second", "output_text": "Second answer.", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )
    first_prefix = [
        {"role": "system", "content": "Always answer with repo-safe constraints."},
        {"role": "developer", "content": "Prefer concise diffs and keep secrets private."},
        {"role": "user", "content": "first"},
    ]
    first = client.chat(messages=first_prefix)
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )

    assert PROVIDER_METADATA_KEY in assistant_message
    assert strip_provider_metadata_from_message(assistant_message) == {
        "role": "assistant",
        "content": "First answer.",
    }
    assert first.provider_metadata is not None
    first_metadata = first.provider_metadata["openai_responses"]
    assert first_metadata["request_plan"]["input_mode"] == "full"

    second = client.chat(
        messages=[
            *first_prefix,
            assistant_message,
            {"role": "user", "content": "second"},
        ],
    )

    assert second.content == "Second answer."
    assert calls[1]["previous_response_id"] == "resp_text"
    assert calls[1]["input"] == [
        {"role": "user", "content": "second"},
    ]
    assert second.provider_metadata is not None
    second_metadata = second.provider_metadata["openai_responses"]["request_plan"]
    assert second_metadata["input_mode"] == "previous_response_id"
    assert second_metadata["previous_response_id_used"] is True
    assert second_metadata["full_input_item_count"] == 5
    assert second_metadata["sent_input_item_count"] == 1
    assert second_metadata["resent_stable_instruction_count"] == 0
    assert second_metadata["stable_prefix_message_count"] == 4
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_plan"]["input_mode"] == "previous_response_id"
    assert summary["request_plan"]["previous_response_id_used"] is True
    assert summary["request_plan"]["request_message_count"] == 5
    assert summary["request_plan"]["full_input_item_count"] == 5
    assert summary["request_plan"]["sent_input_item_count"] == 1
    assert summary["request_plan"]["resent_stable_instruction_count"] == 0
    assert summary["request_plan"]["continuation_anchor_index"] == 3
    assert summary["request_plan"]["request_messages_signature"]
    assert summary["token_reconciliation"]["input_mode"] == "previous_response_id"
    assert (
        summary["token_reconciliation"]["input_estimate_tokens"]
        > (summary["token_reconciliation"]["sent_input_estimate_tokens"])
    )


def test_chat_chained_continuations_never_resend_system_or_developer_messages() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        turn = len(calls)
        return httpx.Response(
            200,
            json={
                "id": f"resp_{turn}",
                "model": "gpt-5.5",
                "output_text": f"Answer {turn}.",
                "output": [
                    {
                        "type": "message",
                        "id": f"msg_{turn}",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"Answer {turn}."}],
                    }
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )
    first_messages = [
        {"role": "system", "content": "Always answer with repo-safe constraints."},
        {"role": "developer", "content": "Prefer concise diffs and keep secrets private."},
        {"role": "user", "content": "first"},
    ]
    first = client.chat(messages=first_messages)
    first_assistant = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )
    second_messages = [
        *first_messages,
        first_assistant,
        {"role": "user", "content": "second"},
    ]
    second = client.chat(messages=second_messages)
    second_assistant = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": second.content},
        second,
    )
    third = client.chat(
        messages=[
            *second_messages,
            second_assistant,
            {"role": "user", "content": "third"},
        ],
    )

    assert third.content == "Answer 3."
    assert len(calls) == 3
    assert "previous_response_id" not in calls[0]
    assert calls[1]["previous_response_id"] == "resp_1"
    assert calls[1]["input"] == [{"role": "user", "content": "second"}]
    assert calls[2]["previous_response_id"] == "resp_2"
    assert calls[2]["input"] == [{"role": "user", "content": "third"}]
    for continuation_body in calls[1:]:
        roles = {
            str(item.get("role") or "")
            for item in continuation_body["input"]
            if isinstance(item, dict)
        }
        assert "system" not in roles
        assert "developer" not in roles
    assert third.provider_metadata is not None
    third_plan = third.provider_metadata["openai_responses"]["request_plan"]
    assert third_plan["input_mode"] == "previous_response_id"
    assert third_plan["resent_stable_instruction_count"] == 0


def test_chat_retries_full_input_when_previous_response_id_is_rejected() -> None:
    reset_provider_telemetry_for_tests()
    calls: list[dict[str, object]] = []
    output_items = [
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "First answer."}],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_text",
                    "model": "gpt-5.5",
                    "output_text": "First answer.",
                    "output": output_items,
                },
            )
        if len(calls) == 2:
            assert body["previous_response_id"] == "resp_text"
            return httpx.Response(
                400,
                json={"error": {"message": "previous_response_id not found"}},
            )
        return httpx.Response(
            200,
            json={"id": "resp_retry", "output_text": "Retried.", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(messages=[{"role": "user", "content": "first"}])
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )

    second = client.chat(
        messages=[
            {"role": "user", "content": "first"},
            assistant_message,
            {"role": "user", "content": "second"},
        ],
    )

    assert second.content == "Retried."
    assert len(calls) == 3
    assert "previous_response_id" not in calls[2]
    assert calls[2]["input"] == [
        {"role": "user", "content": "first"},
        output_items[0],
        {"role": "user", "content": "second"},
    ]
    assert second.provider_metadata is not None
    request_plan = second.provider_metadata["openai_responses"]["request_plan"]
    assert request_plan["input_mode"] == "full_retry_after_previous_response_id_rejected"
    assert request_plan["previous_response_id_used"] is False
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_plan"]["input_mode"] == (
        "full_retry_after_previous_response_id_rejected"
    )
    assert summary["request_plan"]["previous_response_id_used"] is False
    assert summary["request_plan"]["sent_input_item_count"] == 3
    assert summary["request_plan"]["request_messages_signature"]


def test_chat_does_not_use_previous_response_id_when_prefix_changed() -> None:
    calls: list[dict[str, object]] = []
    output_items = [
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "First answer."}],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_text",
                    "model": "gpt-5.5",
                    "output_text": "First answer.",
                    "output": output_items,
                },
            )
        return httpx.Response(
            200,
            json={"id": "resp_second", "output_text": "Second.", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(messages=[{"role": "user", "content": "first"}])
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )

    client.chat(
        messages=[
            {"role": "user", "content": "changed prefix"},
            assistant_message,
            {"role": "user", "content": "second"},
        ],
    )

    assert "previous_response_id" not in calls[1]
    assert calls[1]["input"] == [
        {"role": "user", "content": "changed prefix"},
        output_items[0],
        {"role": "user", "content": "second"},
    ]


def test_chat_hosted_web_search_output_round_trips_from_provider_metadata() -> None:
    calls: list[dict[str, object]] = []
    output_items = [
        {
            "type": "web_search_call",
            "id": "ws_1",
            "status": "completed",
            "action": {
                "type": "search",
                "query": "current docs",
                "sources": [{"title": "Docs", "url": "https://docs.example.com/current"}],
            },
        },
        {
            "type": "message",
            "id": "msg_search",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Use current docs."}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_search",
                    "model": "gpt-5.5",
                    "output_text": "Use current docs.",
                    "output": output_items,
                },
            )
        return httpx.Response(
            200,
            json={"id": "resp_after_search", "output_text": "ok", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(messages=[{"role": "user", "content": "search"}])
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )

    assert PROVIDER_METADATA_KEY in assistant_message
    assert first.provider_metadata is not None
    assert first.provider_metadata["openai_responses"]["web_search_calls"][0]["id"] == "ws_1"

    client.chat(
        messages=[
            {"role": "user", "content": "search"},
            assistant_message,
            {"role": "user", "content": "continue"},
        ],
    )

    assert calls[1]["previous_response_id"] == "resp_search"
    assert calls[1]["input"] == [{"role": "user", "content": "continue"}]


def test_chat_function_call_and_output_round_trip_uses_exact_call_id() -> None:
    calls: list[dict[str, object]] = []
    function_call_item = {
        "type": "function_call",
        "id": "fc_exact",
        "call_id": "call_exact_123",
        "name": "fs_read",
        "arguments": '{"path":"README.md"}',
        "status": "completed",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_call",
                    "model": "gpt-5.5",
                    "output": [function_call_item],
                },
            )
        return httpx.Response(
            200,
            json={"id": "resp_done", "output_text": "Read it.", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "fs_read", "parameters": {"type": "object"}},
            }
        ],
    )
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_exact_123", "content": "file contents"},
        ],
    )

    assert calls[1]["previous_response_id"] == "resp_call"
    assert calls[1]["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_exact_123",
            "output": "file contents",
        },
    ]


def test_chat_completed_empty_output_routes_to_agent_recovery() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp_empty", "status": "completed", "output": []})

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.content == ""
    assert response.tool_calls == []
    assert response.raw["status"] == "completed"
    assert response.provider_metadata is not None
    assert response.provider_metadata["openai_responses"]["output_items"] == []


def test_chat_non_completed_empty_output_remains_explicit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "resp_incomplete", "status": "incomplete", "output": []},
        )

    with pytest.raises(
        LLMError,
        match=(
            "OpenAI Responses returned no assistant text or tool calls "
            r"\(status=incomplete\)"
        ),
    ):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )


def test_chat_completed_reasoning_only_response_routes_to_agent_recovery() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_reasoning_only",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "status": "completed",
                        "encrypted_content": "opaque-provider-state",
                        "summary": [{"type": "summary_text", "text": "Checked the repository."}],
                    }
                ],
            },
        )

    response = _with_openai_reasoning_summary(_client(httpx.MockTransport(handler))).chat(
        messages=[{"role": "user", "content": "inspect"}],
    )

    assert response.content == ""
    assert response.tool_calls == []
    assert [item.text for item in response.reasoning] == ["Checked the repository."]


def test_responses_chat_retries_are_bounded_by_wall_clock_cap() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.RemoteProtocolError("stream ended early", request=request)

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=5,
            base_delay_seconds=10.0,
            max_delay_seconds=120.0,
        ),
        provider_sleep_fn=sleeps.append,
        provider_random_fn=lambda: 0.5,
    )
    client._provider_retry_wall_clock_cap_seconds = 5.0

    with pytest.raises(LLMError, match="stream ended early"):
        client.chat(messages=[{"role": "user", "content": "hello"}])

    assert attempts == 1
    assert sleeps == []


def test_web_search_parses_output_text_citations_and_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "search-model"
        assert body["input"] == "latest httpx release notes"
        assert body["tool_choice"] == "required"
        assert body["include"] == ["web_search_call.action.sources"]
        assert body["tools"] == [
            {
                "type": "web_search",
                "filters": {"allowed_domains": ["github.com", "python.org"]},
                "external_web_access": False,
            }
        ]
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "model": "search-model",
                "output_text": "Use the official release notes page.",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Use the official release notes page.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "httpx Releases",
                                        "url": "https://github.com/encode/httpx/releases",
                                        "start_index": 8,
                                        "end_index": 29,
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "type": "web_search_call",
                        "action": {
                            "queries": ["latest httpx release notes"],
                            "sources": [
                                {
                                    "title": "httpx Releases",
                                    "url": "https://github.com/encode/httpx/releases",
                                },
                                {
                                    "title": "httpx Docs",
                                    "url": "https://www.python-httpx.org/",
                                },
                            ],
                        },
                    },
                ],
            },
        )

    response = _client(httpx.MockTransport(handler)).web_search(
        query="latest httpx release notes",
        allowed_domains=["github.com", "python.org"],
        external_web_access=False,
    )

    assert response.response_id == "resp_123"
    assert response.model == "search-model"
    assert response.answer == "Use the official release notes page."
    assert response.queries == ["latest httpx release notes"]
    assert len(response.citations) == 1
    assert response.citations[0].title == "httpx Releases"
    assert response.citations[0].url == "https://github.com/encode/httpx/releases"
    assert response.citations[0].start_index == 8
    assert response.citations[0].end_index == 29
    assert [source.url for source in response.sources] == [
        "https://github.com/encode/httpx/releases",
        "https://www.python-httpx.org/",
    ]


def test_web_search_falls_back_to_assistant_message_text_when_output_text_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_124",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Read the changelog."},
                            {"type": "output_text", "text": " Then fetch the API docs."},
                        ],
                    },
                    {
                        "type": "web_search_call",
                        "action": {
                            "sources": [
                                {
                                    "title": "Changelog",
                                    "url": "https://github.com/encode/httpx/releases",
                                }
                            ]
                        },
                    },
                ],
            },
        )

    response = _client(httpx.MockTransport(handler)).web_search(query="httpx changelog")

    assert response.answer == "Read the changelog. Then fetch the API docs."
    assert response.citations == []
    assert [source.url for source in response.sources] == [
        "https://github.com/encode/httpx/releases"
    ]


def test_web_search_can_omit_openai_source_include_for_provider_compatibility() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "include" not in body
        assert body["tool_choice"] == "required"
        assert body["tools"] == [{"type": "web_search"}]
        return httpx.Response(
            200,
            json={
                "id": "resp_xai",
                "model": "grok-4",
                "output_text": "Use xAI citations.",
                "citations": [
                    {
                        "title": "xAI Web Search",
                        "url": "https://docs.x.ai/developers/tools/web-search",
                        "start_index": 0,
                        "end_index": 7,
                    }
                ],
            },
        )

    response = _client(httpx.MockTransport(handler)).web_search(
        query="xAI search docs",
        include_source_details=False,
    )

    assert response.answer == "Use xAI citations."
    assert response.citations[0].title == "xAI Web Search"
    assert response.citations[0].url == "https://docs.x.ai/developers/tools/web-search"
    assert response.citations[0].start_index == 0
    assert response.citations[0].end_index == 7
    assert response.sources[0].url == "https://docs.x.ai/developers/tools/web-search"


def test_web_search_http_error_surfaces_provider_message() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "This provider does not support web_search."}},
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ResponsesError, match="Responses web_search unsupported"):
        client.web_search(query="httpx docs")


def test_web_search_non_json_response_is_explicit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ResponsesError, match="non-JSON"):
        client.web_search(query="httpx docs")


def test_web_search_decompression_error_is_explicit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=b'{"output_text":"ok","sources":[{"url":"https://example.com"}]}',
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ResponsesError, match="response decompression failed"):
        client.web_search(query="httpx docs")


def test_web_search_rejects_unsupported_response_shape() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp_125", "model": "search-model", "output": []})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ResponsesError, match="did not return sources"):
        client.web_search(query="httpx docs")


@pytest.fixture
def _include_capability_isolation() -> object:
    """Keep the module-level include capability cache isolated per test."""
    from alysis_code.llm.openai_responses import _RESPONSES_OMIT_INCLUDE_ENDPOINTS

    saved = set(_RESPONSES_OMIT_INCLUDE_ENDPOINTS)
    _RESPONSES_OMIT_INCLUDE_ENDPOINTS.clear()
    yield _RESPONSES_OMIT_INCLUDE_ENDPOINTS
    _RESPONSES_OMIT_INCLUDE_ENDPOINTS.clear()
    _RESPONSES_OMIT_INCLUDE_ENDPOINTS.update(saved)


def _include_rejecting_gateway_handler(
    requests: list[dict[str, object]],
    *,
    success_json: dict[str, object],
) -> object:
    """Gateway that 400s any request carrying the optional ``include`` entries."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if "include" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "include value 'web_search_call.action.sources' "
                            "cannot be produced by this gateway"
                        ),
                    }
                },
            )
        return httpx.Response(200, json=success_json)

    return handler


def test_web_search_retries_without_include_when_gateway_rejects_it(
    _include_capability_isolation: set[str],
) -> None:
    requests: list[dict[str, object]] = []
    handler = _include_rejecting_gateway_handler(
        requests,
        success_json={
            "id": "resp_no_include",
            "model": "search-model",
            "output_text": "Answer from a gateway without source metadata.",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Answer from a gateway without source metadata.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": "Docs",
                                    "url": "https://docs.example.com/answer",
                                }
                            ],
                        }
                    ],
                },
            ],
        },
    )

    response = _client(httpx.MockTransport(handler)).web_search(query="gateway docs")

    assert len(requests) == 2
    assert requests[0]["include"] == ["web_search_call.action.sources"]
    assert "include" not in requests[1]
    assert response.answer == "Answer from a gateway without source metadata."
    assert [source.url for source in response.sources] == ["https://docs.example.com/answer"]


def test_web_search_skips_include_after_gateway_rejected_it_once(
    _include_capability_isolation: set[str],
) -> None:
    requests: list[dict[str, object]] = []
    handler = _include_rejecting_gateway_handler(
        requests,
        success_json={
            "id": "resp_cached_capability",
            "model": "search-model",
            "output_text": "ok",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "ok",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": "Docs",
                                    "url": "https://docs.example.com/cached",
                                }
                            ],
                        }
                    ],
                },
            ],
        },
    )

    client = _client(httpx.MockTransport(handler))
    client.web_search(query="first search")
    assert len(requests) == 2  # include attempt + retry without it

    client.web_search(query="second search")
    assert len(requests) == 3  # exactly one more request, sent without include
    assert "include" not in requests[2]

    # The capability is remembered per endpoint, not per client instance.
    fresh_client = _client(httpx.MockTransport(handler))
    fresh_client.web_search(query="third search")
    assert len(requests) == 4
    assert "include" not in requests[3]


def test_web_search_accepts_absent_sources_when_include_was_rejected(
    _include_capability_isolation: set[str],
) -> None:
    """A gateway that searches but cannot produce source metadata still works."""

    requests: list[dict[str, object]] = []
    handler = _include_rejecting_gateway_handler(
        requests,
        success_json={
            "id": "resp_sourceless",
            "model": "search-model",
            "output_text": "Answer without any source metadata.",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {"queries": ["sourceless docs"]},
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Answer without any source metadata."}
                    ],
                },
            ],
        },
    )

    response = _client(httpx.MockTransport(handler)).web_search(query="sourceless docs")

    assert response.answer == "Answer without any source metadata."
    assert response.sources == []
    assert response.queries == ["sourceless docs"]


def test_web_search_still_rejects_searchless_responses_after_include_was_rejected(
    _include_capability_isolation: set[str],
) -> None:
    """Dropping the include must not accept answers where no search happened.

    A response with no web_search_call output and no citations never searched;
    accepting it would hand the caller an unsourced answer and prevent
    auto-mode from falling back to a working backend.
    """

    requests: list[dict[str, object]] = []
    handler = _include_rejecting_gateway_handler(
        requests,
        success_json={
            "id": "resp_never_searched",
            "model": "search-model",
            "output_text": "A plain model answer with no search behind it.",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "A plain model answer with no search behind it.",
                        }
                    ],
                },
            ],
        },
    )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(ResponsesError, match="did not return sources"):
        client.web_search(query="searchless docs")

    # The capability cache is set, so the next call skips the include — and
    # still refuses the searchless response.
    with pytest.raises(ResponsesError, match="did not return sources"):
        client.web_search(query="searchless docs again")


def test_chat_retries_without_include_when_gateway_rejects_it(
    _include_capability_isolation: set[str],
) -> None:
    requests: list[dict[str, object]] = []
    handler = _include_rejecting_gateway_handler(
        requests,
        success_json={
            "id": "resp_chat_no_include",
            "model": "gateway-model",
            "output_text": "Done.",
            "output": [],
        },
    )

    client = OpenAIResponsesClient(
        base_url="https://gateway.example.com/v1",
        api_key="test-key",
        model="gateway-model",
        web_search_mode="native",
        web_search_adapter="openai_responses",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "Find current docs."}],
        tools=[_web_search_function_tool()],
    )
    assert first.content == "Done."
    assert len(requests) == 2
    assert requests[0]["include"] == ["web_search_call.action.sources"]
    assert "include" not in requests[1]
    # The hosted web_search tool itself stays enabled; only the optional
    # source-metadata enhancement is dropped.
    assert requests[1]["tools"] == [{"type": "web_search", "external_web_access": True}]

    second = client.chat(
        messages=[{"role": "user", "content": "Search again."}],
        tools=[_web_search_function_tool()],
    )
    assert second.content == "Done."
    assert len(requests) == 3
    assert "include" not in requests[2]


def test_chat_drops_temperature_when_model_rejects_it() -> None:
    """GPT-5-class models reject a non-default temperature; the client must omit
    it and retry (and remember the model), so validation + chat both succeed."""
    from alysis_code.llm.openai_responses import (
        _RESPONSES_OMIT_TEMPERATURE_MODELS,
        _responses_temperature_omit_key,
    )

    base_url = "https://api.openai.com/v1"
    model = "gpt-5.5-omit-test"
    _RESPONSES_OMIT_TEMPERATURE_MODELS.discard(_responses_temperature_omit_key(base_url, model))

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Unsupported parameter: 'temperature' is not supported with this "
                            "model. Only the default (1) value is supported."
                        ),
                        "param": "temperature",
                        "code": "unsupported_parameter",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_ok",
                "model": model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url=base_url,
        api_key="test-key",
        model=model,
        temperature=0.4,
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "ping"}])
    assert resp.content == "ok"
    # First attempt carried temperature and 400'd; the retry dropped it and won.
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]

    # The model is now remembered: the next chat omits temperature up front (no
    # wasted 400 round-trip).
    resp2 = client.chat(messages=[{"role": "user", "content": "again"}])
    assert resp2.content == "ok"
    assert len(calls) == 3
    assert "temperature" not in calls[2]


def test_responses_chat_and_count_strip_state_from_different_credential_route() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        sent.append(body)
        if str(request.url).endswith("/responses/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 12})
        return httpx.Response(
            200,
            json={"id": "resp_new", "model": "route-model", "output_text": "ok", "output": []},
        )

    producer = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="credential-a",
        model="route-model",
    )
    consumer = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="credential-b",
        model="route-model",
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "public answer",
            PROVIDER_METADATA_KEY: stamp_provider_metadata_for_route(
                {
                    "openai_responses": {
                        "response_id": "private-response-id",
                        "output_items": [
                            {
                                "type": "reasoning",
                                "encrypted_content": "private-encrypted-state",
                            }
                        ],
                    }
                },
                producer.route_identity,
            ),
        },
        {"role": "user", "content": "follow up"},
    ]

    assert consumer.count_input_tokens(messages=messages) is not None
    assert consumer.chat(messages=messages).content == "ok"

    assert len(sent) == 2
    assert all("private-" not in json.dumps(body) for body in sent)
    assert any("public answer" in json.dumps(body) for body in sent)


def test_responses_extra_headers_override_defaults_case_insensitively() -> None:
    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="fallback-key",
        model="route-model",
        extra_headers={
            "Authorization": "Bearer override",
            "Content-Type": "application/custom+json",
        },
    )

    headers = client._headers("https://api.openai.com/v1/responses")

    assert headers["authorization"] == "Bearer override"
    assert headers["content-type"] == "application/custom+json"
    assert len({name.casefold() for name in headers}) == len(headers)


def test_responses_usage_keeps_cost_only_and_cache_write_only_payloads() -> None:
    from alysis_code.llm.openai_responses import _parse_usage

    cost_only = _parse_usage({"cost": {"currency": "USD", "total_cost": 0.0125}})
    cache_write_only = _parse_usage(
        {"input_tokens_details": {"cache_write_tokens": 40}},
    )
    empty = _parse_usage({})

    # Provider-reported charges are authoritative, so a payload carrying only a
    # cost must not be discarded for lacking token counts.
    assert cost_only is not None
    assert cost_only.provider_cost_usd == 0.0125
    assert cache_write_only is not None
    assert cache_write_only.cache_creation_input_tokens == 40
    assert empty is None
