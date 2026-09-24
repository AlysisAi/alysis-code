from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm import openai_compat
from alysis_code.llm.cache_capabilities import CacheCapabilitySpec
from alysis_code.llm.factory import make_llm_client
from alysis_code.model_registry import ModelRegistry
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)
from alysis_code.request_estimation import estimate_provider_payload_tokens
from alysis_code.usage_tracker import (
    UsageSummary,
    build_usage_record,
    usage_context_from_client_response,
)


@pytest.fixture(autouse=True)
def offline_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))

    def denied(*args, **kwargs):
        raise AssertionError("Cache strategy tests require offline MockTransport")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    reset_provider_telemetry_for_tests()
    yield
    reset_provider_telemetry_for_tests()


def _config(name: str, base_url: str, *, override=None, mode="auto"):
    profile = ProfileSpec(
        name=name,
        protocol="openai_compat",
        base_url=base_url,
        default_model="synthetic-model",
        cache_capability=override,
    )
    cfg = AppConfig(model=profile.default_model, prompt_cache_mode=mode, web_search_mode="off")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def _reply():
    return httpx.Response(
        200,
        json={
            "id": "synthetic-response",
            "model": "synthetic-model",
            "choices": [
                {"message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 5,
                "total_tokens": 1005,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
        },
    )


def _assert_calibration_matches_client(cfg, client, response, messages):
    context = usage_context_from_client_response(
        client=client, response=response, operation="main_llm"
    )
    assert response.usage is not None
    record = build_usage_record(
        role="main",
        requested_model=client.model,
        response_model=response.response_model,
        messages=messages,
        response_content=response.content,
        response_tool_calls=response.tool_calls,
        api_prompt_tokens=response.usage.prompt_tokens,
        api_completion_tokens=response.usage.completion_tokens,
        api_total_tokens=response.usage.total_tokens,
        api_usage=response.usage,
        registry=ModelRegistry(cfg=cfg, api_key="synthetic-key"),
        **context,
    )
    summary = UsageSummary()
    summary.add_record(record)
    static_strategy = client.prompt_cache_policy_metadata["strategy"]
    calibration = summary.recent_calibration_snapshot(
        requested_model=client.model,
        provider_key=context["provider_key"],
        protocol=context["protocol"],
        base_url_host=context["base_url_host"],
        operation="main_llm",
        request_mode=context["request_mode"],
        cache_strategy=static_strategy,
    )
    assert calibration["records"] == 1
    assert record.cache_strategy == static_strategy
    assert context["request_plan"]["cache_strategy"] == static_strategy
    assert last_provider_call_summary()["cache_policy"]["strategy"] == static_strategy


@pytest.mark.parametrize(
    "name,base_url,override,expected",
    [
        ("moonshot", "https://api.moonshot.ai/v1", None, "implicit_provider"),
        ("mistral", "https://api.mistral.ai/v1", None, "mistral_prompt_cache_key"),
        (
            "unlisted-route",
            "https://unlisted.example.test/v1",
            CacheCapabilitySpec(
                strategy="implicit_provider",
                enabled=True,
                supports_prompt_cache_key=True,
                reports_cache_read_tokens=True,
                emits_request_fields=True,
            ),
            "implicit_provider",
        ),
    ],
)
def test_factory_cache_affinity_keeps_declared_strategy_in_usage_and_calibration(
    name, base_url, override, expected
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _reply()

    cfg = _config(name, base_url, override=override)
    client = make_llm_client(
        cfg=cfg,
        api_key="synthetic-key",
        model=cfg.model,
        prompt_cache_key="synthetic-affinity",
        transport=httpx.MockTransport(handler),
    )
    messages = [{"role": "user", "content": "Synthetic request."}]
    response = client.chat(messages=messages)

    assert len(requests) == 1
    assert requests[0]["prompt_cache_key"] == "synthetic-affinity"
    assert client.prompt_cache_policy_metadata["strategy"] == expected
    _assert_calibration_matches_client(cfg, client, response, messages)


@pytest.mark.parametrize("base_metadata", [None, {"strategy": "none"}])
def test_legacy_direct_client_retains_cache_projection_fallback(base_metadata):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _reply()

    client = openai_compat.OpenAICompatClient(
        base_url="https://unlisted.example.test/v1",
        api_key="synthetic-key",
        model="synthetic-model",
        prompt_cache_key="synthetic-affinity",
        prompt_cache_retention="24h",
        prompt_cache_policy_metadata=base_metadata,
        transport=httpx.MockTransport(handler),
    )
    response = client.chat(messages=[{"role": "user", "content": "Synthetic request."}])

    assert requests[0]["prompt_cache_key"] == "synthetic-affinity"
    assert requests[0]["prompt_cache_retention"] == "24h"
    assert last_provider_call_summary()["cache_policy"]["strategy"] == "openai_prompt_cache"
    context = usage_context_from_client_response(
        client=client, response=response, operation="main_llm"
    )
    assert context["cache_strategy"] == "openai_prompt_cache"


def test_cache_off_does_not_activate_declared_strategy_or_key():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _reply()

    cfg = _config("mistral", "https://api.mistral.ai/v1", mode="off")
    client = make_llm_client(
        cfg=cfg,
        api_key="synthetic-key",
        model=cfg.model,
        prompt_cache_key="synthetic-affinity",
        transport=httpx.MockTransport(handler),
    )
    client.chat(messages=[{"role": "user", "content": "Synthetic request."}])

    assert "prompt_cache_key" not in requests[0]
    policy = last_provider_call_summary()["cache_policy"]
    assert policy["strategy"] == client.prompt_cache_policy_metadata["strategy"]
    assert policy["status"] == "disabled"
    assert policy["enabled"] is False
    assert policy["emitted_fields"] == []


def test_key_rejection_preserves_strategy_and_runtime_downgrade():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                422, json={"error": {"param": "prompt_cache_key", "code": "unsupported_parameter"}}
            )
        return _reply()

    cfg = _config("mistral", "https://api.mistral.ai/v1")
    client = make_llm_client(
        cfg=cfg,
        api_key="synthetic-key",
        model=cfg.model,
        prompt_cache_key="synthetic-affinity",
        transport=httpx.MockTransport(handler),
    )
    messages = [{"role": "user", "content": "Synthetic request."}]
    first = client.chat(messages=messages)
    first_policy = last_provider_call_summary()["cache_policy"]
    second = client.chat(messages=messages)
    second_policy = last_provider_call_summary()["cache_policy"]

    assert len(requests) == 3
    assert requests[0]["prompt_cache_key"] == "synthetic-affinity"
    assert all("prompt_cache_key" not in request for request in requests[1:])
    for policy in (first_policy, second_policy):
        assert policy["strategy"] == "mistral_prompt_cache_key"
        assert policy["enabled"] is False
        assert policy["emitted_fields"] == []
        assert policy["runtime_disabled_fields"] == ["prompt_cache_key"]
        assert policy["capability_downgrade"] == "session_local_provider_rejection"
    for response in (first, second):
        _assert_calibration_matches_client(cfg, client, response, messages)


def test_counting_and_chat_keep_strategy_while_applying_cache_control(monkeypatch):
    requests = []
    observed_policies = []
    apply_breakpoint = openai_compat.apply_openai_compatible_cache_control_breakpoint

    def observed_breakpoint(messages, *, cache_policy):
        observed_policies.append(dict(cache_policy))
        return apply_breakpoint(messages, cache_policy=cache_policy)

    def handler(request):
        requests.append(json.loads(request.content))
        return _reply()

    monkeypatch.setattr(
        openai_compat, "apply_openai_compatible_cache_control_breakpoint", observed_breakpoint
    )
    client = openai_compat.OpenAICompatClient(
        base_url="https://unlisted.example.test/v1",
        api_key="synthetic-key",
        model="synthetic-model",
        prompt_cache_key="synthetic-affinity",
        prompt_cache_request_field_values={"cache_control": "ephemeral"},
        prompt_cache_policy_metadata={
            "strategy": "qwen_cache_control_blocks",
            "enabled": True,
            "status": "enabled",
            "allowed_fields": ["prompt_cache_key", "cache_control"],
            "emitted_fields": ["prompt_cache_key", "cache_control"],
            "min_tokens": 1,
        },
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "system", "content": "Stable synthetic instructions."},
        {"role": "user", "content": "Synthetic request."},
    ]
    count = client.count_input_tokens(messages=messages)
    client.chat(messages=messages)

    assert len(observed_policies) == 2
    assert all(policy["strategy"] == "qwen_cache_control_blocks" for policy in observed_policies)
    assert requests[0]["prompt_cache_key"] == "synthetic-affinity"
    assert requests[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert count.input_tokens == estimate_provider_payload_tokens(
        {"messages": requests[0]["messages"]}
    )
