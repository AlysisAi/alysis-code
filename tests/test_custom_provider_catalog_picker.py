"""Custom profile discovery uses advertised inventory without choosing a protocol."""

from __future__ import annotations

import copy
import socket
from pathlib import Path

import httpx
import pytest

from alysis_code.cli_impl import config_menu, setup_wizard
from alysis_code.config import AppConfig, ConfigError
from alysis_code.profile_presets import get_preset
from alysis_code.profiles import ProfileSpec
from alysis_code.provider_model_catalog import ProviderModelCatalogError, discover_provider_models


@pytest.fixture(autouse=True)
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))

    def denied(*_args, **_kwargs):
        raise AssertionError("No external network or model calls in catalog picker tests")

    def no_saved_key(*_args, **_kwargs):
        raise ConfigError("No saved test credential")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(config_menu, "resolve_api_key", no_saved_key)


def profile() -> ProfileSpec:
    return ProfileSpec(
        name="custom-gateway",
        base_url="https://gateway.example/v1",
        default_model="saved-model",
    )


def state_for(value: ProfileSpec) -> config_menu.ConfigMenuState:
    cfg = AppConfig(model=value.default_model, base_url=value.base_url)
    cfg.extra_fields = {"profiles": {value.name: value.to_dict()}, "active_profile": value.name}
    return config_menu.ConfigMenuState.from_cfg(cfg)


def use_catalog(monkeypatch: pytest.MonkeyPatch, payload: dict) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json=payload)

    def discover(**kwargs):
        return discover_provider_models(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(config_menu, "discover_provider_models", discover)
    return requests


def test_custom_picker_merges_real_catalog_once_and_keeps_unknown_compatibility(monkeypatch):
    requests = use_catalog(
        monkeypatch,
        {
            "object": "list",
            "data": [
                {"id": "saved-model", "object": "model"},
                {"id": "new-model", "object": "model", "owned_by": "gateway"},
                {"id": "namespace/another-model", "object": "model"},
                {"id": "known-chat", "supported_endpoints": ["/chat/completions"]},
                {"id": "embedding-model", "mode": "embedding"},
            ],
        },
    )
    state = state_for(profile())
    state.set_field("new_api_key", "test-only-key")
    original_profiles = copy.deepcopy(state.profiles)
    first = config_menu._default_model_rows(state)
    assert first == config_menu._default_model_rows(state)
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-only-key"
    ids = [row[0] for row in first]
    assert ids.count("saved-model") == 1
    assert "embedding-model" not in ids
    assert ids.index(config_menu._CUSTOM_MODEL_VALUE) < ids.index("new-model")
    unknown = next(row for row in first if row[0] == "new-model")
    known = next(row for row in first if row[0] == "known-chat")
    assert "chat compatibility unverified" in unknown[1]
    assert "does not declare endpoint compatibility" in unknown[2]
    assert "live provider catalog" in unknown[2] and "NVIDIA" not in unknown[2]
    namespaced = next(row for row in first if row[0] == "namespace/another-model")
    assert "hosted model from" not in namespaced[2]
    assert "unverified" not in known[1]
    assert state.profiles == original_profiles
    assert state.fields["model"] == "saved-model"
    assert not state._provider_model_catalog_warning


@pytest.mark.parametrize("change", ["credential", "protocol", "headers", "url"])
def test_custom_picker_cache_tracks_request_identity(monkeypatch, change):
    requests = use_catalog(monkeypatch, {"data": [{"id": "listed-model"}]})
    state = state_for(profile())
    state.set_field("new_api_key", "test-key-one")
    config_menu._default_model_rows(state)
    selected = state.profiles[state.active_profile]
    if change == "credential":
        state.set_field("new_api_key", "test-key-two")
    elif change == "protocol":
        selected["protocol"] = "openai_responses"
    elif change == "headers":
        selected["extra_headers"] = {"X-Catalog-Account": "other-account"}
    else:
        selected["base_url"] = "https://other.example/v1"
    config_menu._default_model_rows(state)
    assert len(requests) == 2
    assert config_menu._default_model_rows(state)
    assert len(requests) == 2


def test_custom_public_catalog_can_be_listed_without_a_key(monkeypatch):
    requests = use_catalog(monkeypatch, {"data": [{"id": "public-model"}]})
    state = state_for(profile())
    assert "public-model" in [row[0] for row in config_menu._default_model_rows(state)]
    assert "authorization" not in requests[0].headers


def test_custom_picker_failure_is_cached_sanitized_and_retains_manual_entry(monkeypatch):
    calls = []

    def fail(**_kwargs):
        calls.append(True)
        raise ProviderModelCatalogError("untrusted-response-must-not-be-rendered")

    monkeypatch.setattr(config_menu, "discover_provider_models", fail)
    state = state_for(profile())
    rows = config_menu._default_model_rows(state)
    warning = state._provider_model_catalog_warning
    assert "unavailable" in warning
    assert "untrusted-response" not in warning
    assert [row[0] for row in rows] == ["saved-model", config_menu._CUSTOM_MODEL_VALUE]
    assert config_menu._default_model_rows(state) == rows
    assert state._provider_model_catalog_warning == warning
    assert len(calls) == 1
    state.add_profile_spec(
        ProfileSpec(
            name="deepseek", base_url="https://api.deepseek.com", default_model="deepseek-v4-pro"
        )
    )
    config_menu._default_model_rows(state)
    assert state._provider_model_catalog_warning == ""
    assert len(calls) == 1
    state.set_active_profile_name("custom-gateway")
    config_menu._default_model_rows(state)
    assert state._provider_model_catalog_warning == warning
    assert len(calls) == 2


@pytest.mark.parametrize("preset_key", [None, "custom"])
def test_custom_setup_discovers_catalog_and_shows_manual_entry_before_inventory(preset_key):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "saved-model"}, {"id": "new-model", "object": "model"}]},
        )

    value = profile()
    step = setup_wizard._ProfileStepResult(
        profile=value, label="Custom gateway", preset=get_preset(preset_key) if preset_key else None
    )
    options, warning = setup_wizard._discover_setup_provider_models(
        step, api_key="test-key", transport=httpx.MockTransport(handler)
    )
    assert len(requests) == 1
    assert not warning
    rows = setup_wizard._model_picker_rows(step, discovered_models=options)
    ids = [row[0] for row in rows]
    assert ids == ["saved-model", setup_wizard._CUSTOM_MODEL_VALUE, "new-model"]
    assert "chat compatibility unverified" in rows[-1][1]
    assert "live provider catalog" in rows[-1][2]
    assert "NVIDIA" not in rows[-1][2]
    assert step.profile.protocol == "openai_compat"
    assert step.profile.default_model == "saved-model"


def test_custom_setup_failure_preserves_fallback_and_sanitizes_provider_body():
    step = setup_wizard._ProfileStepResult(profile=profile(), label="Custom gateway", preset=None)
    options, warning = setup_wizard._discover_setup_provider_models(
        step,
        api_key="test-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text="private-provider-body")),
    )
    assert options == ()
    assert "unavailable" in warning and "private-provider-body" not in warning
    assert [row[0] for row in setup_wizard._model_picker_rows(step)] == [
        "saved-model",
        setup_wizard._CUSTOM_MODEL_VALUE,
    ]


def test_existing_non_nvidia_setup_preset_does_not_gain_discovery():
    preset = get_preset("deepseek")
    assert preset is not None
    step = setup_wizard._ProfileStepResult(profile=profile(), label=preset.label, preset=preset)

    def unexpected(_request):
        pytest.fail("Existing preset must keep its curated picker behavior")

    assert setup_wizard._discover_setup_provider_models(
        step, api_key="test-key", transport=httpx.MockTransport(unexpected)
    ) == ((), "")
