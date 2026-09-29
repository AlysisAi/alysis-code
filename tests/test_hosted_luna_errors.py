from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.llm_error_display import friendly_llm_error_message


@pytest.mark.parametrize(
    "client_type, model",
    [(OpenAIResponsesClient, "gpt-6-luna"), (AnthropicMessagesClient, "claude-sonnet-5-5")],
)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "code, message, expected",
    [
        (
            "rate_limit_exceeded",
            "Your account has reached the 20 requests per minute limit. Retry in 42 seconds. No generation was started or credits charged.",
            "Your account has reached the 20 requests per minute limit. Retry in 42 seconds.",
        ),
        (
            "hosted_capacity_exceeded",
            "Hosted model capacity is busy. Retry in 43 seconds. No generation was started or credits charged.",
            "Hosted model capacity is busy. Retry in 43 seconds.",
        ),
        (
            "hosted_capacity_exceeded",
            "Hosted models are at capacity. Please retry in a few seconds.",
            "The Alysis hosted model is unavailable or at capacity.",
        ),
        (
            "rate_limit_exceeded",
            "5-hour fair-use limit reached (25 credits). Retry as usage ages out.",
            "5-hour fair-use limit reached (25 credits). Retry as usage ages out.",
        ),
        (
            "rate_limit_exceeded",
            "Weekly fair-use limit reached (50 credits in 7 days).",
            "Weekly fair-use limit reached (50 credits in 7 days).",
        ),
        (
            "rate_limit_exceeded",
            "Four hosted requests are already running for your account. Retry shortly.",
            "Four hosted requests are already running for your account. Retry shortly.",
        ),
        (
            "rate_limit_exceeded",
            "Your account has reached its hosted request limit. Retry shortly.",
            "Your account has reached its hosted request limit. Retry shortly.",
        ),
        (
            "rate_limit_exceeded",
            "Your available credit allowance is exhausted. Retry when it refills.",
            "Your available credit allowance is exhausted. Retry when it refills.",
        ),
    ],
)
def test_hosted_clients_display_gateway_capacity_and_credit_reason(
    client_type, model, stream, code, message, expected
):
    payload = {"error": {"code": code, "message": message, "type": code}}
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, json=payload, headers={"Retry-After": "5"})

    client = client_type(
        base_url="https://gateway.example.test/v1",
        api_key="slk_test",
        model=model,
        provider_key="alysis",
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(LLMError) as raised:
        client.chat(messages=[{"role": "user", "content": "hi"}], stream=stream)
    assert len(requests) == 1
    assert raised.value.provider_status_code == 429
    assert json.loads(raised.value.provider_error_body) == payload
    displayed = friendly_llm_error_message(raised.value)
    assert displayed.startswith(expected)
    assert "rate-limiting this session" not in displayed
    if client_type is AnthropicMessagesClient:
        assert friendly_llm_error_message(str(raised.value)) == displayed
