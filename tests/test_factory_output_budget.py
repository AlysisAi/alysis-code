from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.litellm_static_provider import LiteLLMStaticMetadata
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.factory import make_llm_client
from alysis_code.model_registry import ModelRegistry
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.token_budget import compute_input_budget


@pytest.fixture(autouse=True)
def isolated_offline_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    for name in ("ALYSIS_MAX_OUTPUT_TOKENS", "ALYSIS_CONTEXT_WINDOW"):
        monkeypatch.delenv(name, raising=False)

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Real network access is forbidden in this factory test")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)


def _config(*, model: str, output_capacity: int | None) -> AppConfig:
    cfg = AppConfig(model=model)
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="messages-gateway",
            protocol="anthropic_messages",
            base_url="https://gateway.example.test/v1",
        ),
    )
    set_active_profile(cfg, "messages-gateway")
    if output_capacity is not None:
        cfg.extra_fields["model_metadata_overrides"] = {
            "models": {
                model: {
                    "context_window_tokens": 262_144,
                    "max_output_tokens": output_capacity,
                }
            }
        }
    return cfg


def _transport(captured: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(
            200,
            json={
                "id": "msg_budget_test",
                "model": payload["model"],
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("capacity", "expected"),
    [(2048, 2048), (24_000, 24_000), (96_000, 96_000), (128_000, 128_000), (None, 32_000)],
)
def test_factory_uses_known_output_capacity_or_shared_unknown_default(
    capacity: int | None, expected: int
) -> None:
    model = "arbitrary-budget-test-model"
    cfg = _config(model=model, output_capacity=capacity)
    before = cfg.model_dump(mode="json")
    captured: list[dict[str, Any]] = []

    # Separate session clients, including a child, use the same factory policy.
    for session_id in ("parent-session", "child-session"):
        client = make_llm_client(
            cfg=cfg.model_copy(deep=True),
            api_key="test-key",
            model=model,
            session_id=session_id,
            transport=_transport(captured),
        )
        assert isinstance(client, AnthropicMessagesClient)
        assert client.default_max_tokens == expected
        response = client.chat(messages=[{"role": "user", "content": "Continue."}])
        assert response.content == "Done."

    assert [request["max_tokens"] for request in captured] == [expected, expected]
    assert cfg.model_dump(mode="json") == before
    meta = ModelRegistry(cfg=cfg).get(model, include_provider_auth=False)
    assert meta.max_output_tokens == expected
    assert compute_input_budget(meta) + expected + 512 == meta.context_window_tokens


def test_catalog_output_limit_is_used_even_when_other_metadata_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "arbitrary-catalog-model"
    monkeypatch.setattr(
        "alysis_code.model_registry.resolve_litellm_static_metadata",
        lambda *args, **kwargs: LiteLLMStaticMetadata(
            model_key=model,
            context_window_tokens=262_144,
            max_output_tokens=128_000,
            supports_vision=None,
            input_cost_per_token=None,
            output_cost_per_token=None,
            raw_metadata={},
            error=None,
        ),
    )
    captured: list[dict[str, Any]] = []
    client = make_llm_client(
        cfg=_config(model=model, output_capacity=None),
        api_key="test-key",
        model=model,
        transport=_transport(captured),
    )

    client.chat(messages=[{"role": "user", "content": "Continue."}])

    assert captured[0]["max_tokens"] == 128_000


@pytest.mark.parametrize("explicit_limit", [128, 40_000])
def test_explicit_request_limit_remains_authoritative(explicit_limit: int) -> None:
    model = "arbitrary-budget-test-model"
    captured: list[dict[str, Any]] = []
    client = make_llm_client(
        cfg=_config(model=model, output_capacity=96_000),
        api_key="test-key",
        model=model,
        transport=_transport(captured),
    )
    client.chat(messages=[{"role": "user", "content": "Continue."}], max_tokens=explicit_limit)

    assert captured[0]["max_tokens"] == explicit_limit
    assert client.default_max_tokens == 96_000


def test_direct_adapter_retains_its_standalone_default() -> None:
    captured: list[dict[str, Any]] = []
    client = AnthropicMessagesClient(
        base_url="https://gateway.example.test/v1",
        api_key="test-key",
        model="arbitrary-budget-test-model",
        transport=_transport(captured),
    )
    client.chat(messages=[{"role": "user", "content": "Continue."}])

    assert captured[0]["max_tokens"] == 4096
