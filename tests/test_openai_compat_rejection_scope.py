from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.base import effective_tools_for_client
from alysis_code.llm.openai_compat import LLMError, OpenAICompatClient
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]
TOOL_CALL = {
    "id": "call_read",
    "type": "function",
    "function": {"name": "read_file", "arguments": '{"path":"demo.py"}'},
}


@pytest.fixture(autouse=True)
def isolated_client_tests(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))

    def deny_network(*args, **kwargs):
        raise AssertionError("Only mocked HTTP transport is permitted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_network)
    reset_provider_telemetry_for_tests()
    yield
    reset_provider_telemetry_for_tests()


def success_response(message, *, stream):
    usage = {
        "prompt_tokens": 32,
        "prompt_tokens_details": {"cached_tokens": 16},
        "completion_tokens": 5,
        "total_tokens": 37,
    }
    if not stream:
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": usage})
    delta = dict(message)
    if "tool_calls" in delta:
        delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(delta["tool_calls"])]
    events = [
        {"choices": [{"index": 0, "delta": delta}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
    ]
    content = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "error_body",
    [
        {
            "error": {
                "type": "invalid_request_error",
                "message": (
                    "Upstream request failed: [invalid_request_error] "
                    "invalid temperature: only 1 is allowed for this model"
                ),
            }
        },
        "invalid temperature: only 1 is allowed for this model",
        {
            "error": {
                "param": "temperature",
                "message": "Invalid temperature for this model when tools are supplied; only 1 is allowed",
            }
        },
    ],
    ids=["upstream-envelope", "plain-body", "explicit-param-with-tools-context"],
)
def test_temperature_retry_preserves_tools_and_followup(stream, error_body):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("temperature") != 1:
            if isinstance(error_body, dict):
                return httpx.Response(400, json=error_body)
            return httpx.Response(400, text=error_body)
        if not payload.get("tools"):
            return success_response(
                {"role": "assistant", "content": "tools unavailable"}, stream=stream
            )
        if payload["messages"][-1]["role"] == "tool":
            return success_response({"role": "assistant", "content": "done"}, stream=stream)
        return success_response({"role": "assistant", "tool_calls": [TOOL_CALL]}, stream=stream)

    client = OpenAICompatClient(
        base_url="https://example.invalid/v1",
        api_key="test",
        model="test-model",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )
    messages = [{"role": "user", "content": "Inspect demo.py"}]
    first = client.chat(messages=messages, tools=TOOLS, tool_choice="auto", stream=stream)
    assert [(call.name, call.arguments) for call in first.tool_calls] == [
        ("read_file", {"path": "demo.py"})
    ]
    assert effective_tools_for_client(client, TOOLS) == TOOLS
    first_summary = last_provider_call_summary()
    assert first_summary["request_shape"]["tool_count"] == 1
    assert first_summary["request_shape"]["input_mode"] == "full"

    second = client.chat(
        messages=[
            *messages,
            {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]},
            {"role": "tool", "tool_call_id": "call_read", "content": "file contents"},
        ],
        tools=TOOLS,
        tool_choice="auto",
        stream=stream,
    )
    assert second.content == "done"
    assert len(requests) == 3
    assert [request["temperature"] for request in requests] == [0.2, 1, 1]
    assert all(request["tools"] == TOOLS for request in requests)
    assert all(request["tool_choice"] == "auto" for request in requests)
    for response in (first, second):
        assert response.provider_metadata["openai_compat"]["request_plan"]["tool_count"] == 1
        assert response.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"
        assert not response.provider_metadata["transport"].get("tools_omitted")
    assert first.provider_metadata["transport"]["temperature_retry_count"] == 1
    assert second.provider_metadata["transport"]["temperature_adjustment_reason"] == (
        "cached_provider_rejection"
    )


@pytest.mark.parametrize(
    "error_body",
    [
        {"error": {"param": "max_tokens", "message": "Invalid limit for this model with tools"}},
        {"error": {"param": "model", "message": "Unknown model"}},
        {"error": {"message": "Invalid request for this model"}},
    ],
)
def test_unrelated_rejection_does_not_disable_tools_or_retry(error_body):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json=error_body)
        return success_response({"role": "assistant", "content": "done"}, stream=False)

    client = OpenAICompatClient(
        base_url="https://example.invalid/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError, match="LLM error 400"):
        client.chat(messages=[{"role": "user", "content": "Inspect"}], tools=TOOLS)
    assert len(requests) == 1
    assert effective_tools_for_client(client, TOOLS) == TOOLS
    client.chat(messages=[{"role": "user", "content": "Inspect again"}], tools=TOOLS)
    assert all(request["tools"] == TOOLS for request in requests)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "error_body",
    [
        {"error": {"param": "tools", "code": "unsupported_parameter", "message": "不支持"}},
        {"error": {"message": "Tools are not supported by this model for function calling"}},
    ],
    ids=["explicit-tool-param", "tool-rejection-message"],
)
def test_actual_tool_rejection_still_disables_tools_for_retry_and_followup(stream, error_body):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "tools" in payload:
            return httpx.Response(400, json=error_body)
        return success_response({"role": "assistant", "content": "done"}, stream=stream)

    client = OpenAICompatClient(
        base_url="https://example.invalid/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "Inspect"}],
        tools=TOOLS,
        tool_choice="auto",
        stream=stream,
    )
    second = client.chat(
        messages=[{"role": "user", "content": "Follow up"}],
        tools=TOOLS,
        tool_choice="auto",
        stream=stream,
    )
    assert first.content == second.content == "done"
    assert len(requests) == 3
    assert requests[0]["tools"] == TOOLS
    assert all("tools" not in request and "tool_choice" not in request for request in requests[1:])
    assert effective_tools_for_client(client, TOOLS) is None
    assert first.provider_metadata["transport"]["tools_retry_used"] is True
    assert first.provider_metadata["openai_compat"]["request_plan"]["tool_count"] == 0
    assert first.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == (
        "tool_calling_fallback"
    )
