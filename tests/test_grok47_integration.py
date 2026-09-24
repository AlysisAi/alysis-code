"""Grok 4.7 catalog -> TUI selection -> xAI request regression coverage."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from alysis_code.cli_impl.tui import setup_flow as setup_mod
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.cli_impl.tui.setup_flow import SetupFlow
from alysis_code.config import AppConfig, load_config, load_persisted_profile_keys
from alysis_code.llm.factory import make_llm_client
from alysis_code.model_registry import OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE, ModelRegistry
from alysis_code.profile_presets import (
    get_preset,
    make_profile_from_preset,
    model_options_for_preset,
)
from alysis_code.profiles import get_active_profile
from alysis_code.reasoning_contracts import ALWAYS_ON, OFF_IMPOSSIBLE, reasoning_contract_for


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "ALYSIS_API_KEY",
        "XAI_API_KEY",
        "OPENAI_API_KEY",
        "ALYSIS_CONTEXT_WINDOW",
        "ALYSIS_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


def _config(model="grok-4.7"):
    preset = get_preset("xai")
    assert preset is not None
    profile = replace(make_profile_from_preset(preset, name="xai"), default_model=model)
    cfg = AppConfig(model=model, base_url=profile.base_url)
    cfg.extra_fields = {"profiles": {"xai": profile.to_dict()}, "active_profile": "xai"}
    return cfg


def test_catalog_contains_public_model_with_official_metadata(isolated_config):
    preset = get_preset("xai")
    assert preset is not None
    assert preset.suggested_models[0] == preset.validation_model == "grok-4.7"
    assert model_options_for_preset(preset)[0][:2] == ("grok-4.7", "Grok 4.7")
    assert "grok-4.7-fast" not in preset.suggested_models
    assert preset.base_url == "https://api.x.ai/v1"
    assert preset.api_key_env == "XAI_API_KEY"

    meta = ModelRegistry(cfg=_config()).get("grok-4.7")
    assert meta.context_window_tokens == 500_000
    # Alysis reserves 1/8 of a shared context window for output.
    assert meta.max_output_tokens == 62_500
    assert meta.supports_vision is True
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == 0.000002
    assert meta.cache_read_input_cost_per_token == 0.0000005
    assert meta.output_cost_per_token == meta.reasoning_output_cost_per_token == 0.000006
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:xai"
    )
    assert meta.raw_metadata["catalog_sources"] == [
        "https://docs.x.ai/developers/models/grok-4.7",
        "https://docs.x.ai/developers/pricing",
    ]
    assert not any("fallback context/max_output" in warning for warning in meta.warnings)

    contract = reasoning_contract_for("xai", "grok-4.7")
    assert contract.mode == ALWAYS_ON
    assert contract.off == OFF_IMPOSSIBLE
    assert contract.values == ("low", "medium", "high", "xhigh")
    assert contract.default == "high"
    assert contract.emits_flat_reasoning_effort


def test_setup_offers_grok47_without_live_catalog_and_saves_xai_profile(
    isolated_config, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        setup_mod._wiz,
        "_validate_api_key",
        lambda **_kwargs: setup_mod._wiz._ApiKeyValidationResult(status="validated"),
    )
    monkeypatch.setattr(
        setup_mod._wiz,
        "_discover_setup_provider_models",
        lambda *_args, **_kwargs: ((), "Provider catalog unavailable"),
    )
    monkeypatch.setattr(
        "alysis_code.sandbox_doctor.diagnose_sandbox",
        lambda *_args, **_kwargs: SimpleNamespace(
            ready=True,
            status="ready",
            selected_backend="docker",
            docker_image="test",
            can_pull=False,
        ),
    )

    flow = SetupFlow()
    flow.advance_message()
    assert "xai" in {row.value for row in flow.screen().rows}
    flow.choose("xai")
    flow.submit_input("xai-test-key")
    flow.run_busy()
    assert flow.stage == "model"
    row = next(row for row in flow.screen().rows if row.value == "grok-4.7")
    assert row.label == "Grok 4.7"
    flow.choose(row.value)
    flow.run_busy()
    assert flow.stage == "workspace"
    flow.submit_input(str(tmp_path))
    while flow.current_mode() == "busy":
        flow.run_busy()
    assert flow.stage == "complete"
    saved = load_config()
    profile = get_active_profile(saved)
    assert saved.model == profile.default_model == "grok-4.7"
    assert profile.base_url == "https://api.x.ai/v1"
    assert profile.api_key_env == "XAI_API_KEY"
    assert profile.protocol == "openai_compat"
    assert profile.web_search_adapter == "xai_responses"
    assert load_persisted_profile_keys()[profile.name] == "xai-test-key"


def test_config_upgrade_persists_model_and_effort_used_by_request(isolated_config):
    flow = ConfigFlow(cfg=_config("grok-4.6"))
    flow.choose("default")
    # Opening the picker preserves an existing user's selected model.
    assert flow.screen().rows[flow.index].value == "grok-4.6"
    assert "grok-4.7" in {row.value for row in flow.screen().rows}
    flow.choose("grok-4.7")
    assert flow.stage == "model_base_url"
    assert flow.screen().input_default == "https://api.x.ai/v1"
    flow.submit_input("")
    assert flow.stage == "model_thinking"
    assert {row.value for row in flow.screen().rows} == {"auto", "low", "medium", "high", "xhigh"}
    flow.choose("xhigh")
    flow.choose("__save__")
    flow.run_busy()
    assert flow.success
    saved = load_config()
    assert saved.model == "grok-4.7"

    def handler(request):
        assert str(request.url) == "https://api.x.ai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer xai-test-key"
        body = json.loads(request.content)
        assert body["model"] == "grok-4.7"
        assert body["reasoning_effort"] == "xhigh"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = make_llm_client(
        cfg=saved,
        api_key="xai-test-key",
        model=saved.model,
        transport=httpx.MockTransport(handler),
    )
    assert client.chat(messages=[{"role": "user", "content": "ping"}]).content == "ok"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", None])
def test_grok47_requests_support_effort_tools_images_and_cache(isolated_config, stream, effort):
    tool_call = {
        "id": "call_read",
        "type": "function",
        "function": {"name": "fs_read", "arguments": '{"path":"README.md"}'},
    }
    tool = {
        "type": "function",
        "function": {"name": "fs_read", "parameters": {"type": "object", "properties": {}}},
    }
    image = {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}}

    def handler(request):
        assert request.headers["x-grok-conv-id"].startswith("alysis:xai:")
        body = json.loads(request.content)
        assert body["model"] == "grok-4.7"
        assert body.get("reasoning_effort") == effort
        assert body["tools"] == [tool]
        assert body["messages"][0]["content"][1] == image
        assert (
            not {
                "thinking",
                "enable_thinking",
                "reasoning",
                "stop",
                "presence_penalty",
                "frequency_penalty",
            }
            & body.keys()
        )
        if stream:
            assert body["stream"] is True
            event = {"choices": [{"delta": {"tool_calls": [{"index": 0, **tool_call}]}}]}
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "", "tool_calls": [tool_call]}}],
            },
        )

    cfg = _config()
    cfg.prompt_cache_mode = "auto"
    client = make_llm_client(
        cfg=cfg,
        api_key="xai-test-key",
        model="grok-4.7",
        reasoning_effort=effort,
        prompt_cache_namespace="grok47-test",
        transport=httpx.MockTransport(handler),
    )
    response = client.chat(
        messages=[{"role": "user", "content": [{"type": "text", "text": "Read this"}, image]}],
        tools=[tool],
        stream=stream,
    )
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "fs_read"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
