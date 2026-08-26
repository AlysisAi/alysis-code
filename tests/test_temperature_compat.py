from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.temperature_compat import (
    ANTHROPIC_DEPRECATED_SAMPLING_PARAMETERS,
    DEEPSEEK_THINKING_TEMPERATURE_UNSUPPORTED,
    GEMINI_3_DEFAULT_TEMPERATURE,
    QWEN_QVQ_DEFAULT_TEMPERATURE,
    documented_temperature_omit_reason,
)


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "anthropic/claude-opus-4.8",
        "claude-opus-4-9",
        "claude-opus-5",
        "claude-sonnet-5",
    ],
)
def test_anthropic_models_with_deprecated_sampling_omit_temperature(model: str) -> None:
    assert documented_temperature_omit_reason(model) == ANTHROPIC_DEPRECATED_SAMPLING_PARAMETERS


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-6",
        "claude-opus-4-5-20251101",
        "claude-opus-4-20250514",
    ],
)
def test_anthropic_models_that_support_temperature_are_not_overridden(model: str) -> None:
    assert documented_temperature_omit_reason(model) is None


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
    ],
)
def test_gemini_3_uses_provider_default_temperature(model: str) -> None:
    assert documented_temperature_omit_reason(model) == GEMINI_3_DEFAULT_TEMPERATURE


def test_gemini_2_5_keeps_explicit_temperature_support() -> None:
    assert documented_temperature_omit_reason("gemini-2.5-flash") is None


def test_deepseek_v4_omits_temperature_only_while_thinking() -> None:
    assert (
        documented_temperature_omit_reason(
            "deepseek-v4-pro",
            provider_key="deepseek",
            thinking_enabled=None,
        )
        == DEEPSEEK_THINKING_TEMPERATURE_UNSUPPORTED
    )
    assert (
        documented_temperature_omit_reason(
            "deepseek-v4-pro",
            provider_key="deepseek",
            thinking_enabled=True,
        )
        == DEEPSEEK_THINKING_TEMPERATURE_UNSUPPORTED
    )
    assert (
        documented_temperature_omit_reason(
            "deepseek-v4-pro",
            provider_key="deepseek",
            thinking_enabled=False,
        )
        is None
    )


def test_qvq_uses_its_provider_default_temperature() -> None:
    assert documented_temperature_omit_reason("qwen/qvq-max") == QWEN_QVQ_DEFAULT_TEMPERATURE


@pytest.mark.parametrize(
    ("model", "provider_key"),
    [
        ("anthropic/claude-opus-4.8", "openrouter"),
        ("google/gemini-3.5-flash", "openrouter"),
        ("gemini-3.1-pro-preview", "gemini"),
    ],
)
def test_openai_compatible_gateways_proactively_omit_documented_temperature(
    model: str,
    provider_key: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        model=model,
        provider_key=provider_key,
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    assert client.chat(messages=[{"role": "user", "content": "hello"}]).content == "ok"
    assert "temperature" not in captured


def test_deepseek_v4_temperature_is_available_when_thinking_is_disabled() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model="deepseek-v4-pro",
        provider_key="deepseek",
        temperature=0.2,
        enable_thinking=False,
        transport=httpx.MockTransport(handler),
    )

    assert client.chat(messages=[{"role": "user", "content": "hello"}]).content == "ok"
    assert captured["temperature"] == 0.2
    assert captured["thinking"] == {"type": "disabled"}


def test_deepseek_v4_default_thinking_omits_temperature() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model="deepseek-v4-pro",
        provider_key="deepseek",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    assert client.chat(messages=[{"role": "user", "content": "hello"}]).content == "ok"
    assert "temperature" not in captured
