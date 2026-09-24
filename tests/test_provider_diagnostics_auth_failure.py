"""Unavailable auth must be diagnosable without weakening native launch errors."""

from __future__ import annotations

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)

import alysis_code.provider_diagnostics as diagnostics_mod
from alysis_code.config import ApiKeyResolution, AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth import ProviderAuthError


def _cfg(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    # A diagnosis of this synthetic unavailable adapter needs no credential store.
    monkeypatch.setattr(
        diagnostics_mod,
        "_resolve_active_profile_api_key",
        lambda *_args, **_kwargs: ApiKeyResolution(key=None, source="none"),
    )
    cfg = AppConfig(model="offline-auth-review", web_search_mode="off")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="auth-review",
            protocol="openai_responses",
            base_url="https://offline.invalid/v1",
            auth_provider="unregistered-review-adapter",
            default_model="offline-auth-review",
        ),
    )
    set_active_profile(cfg, "auth-review")
    return cfg


def test_unavailable_auth_adapter_remains_diagnosable_but_cannot_launch(monkeypatch) -> None:
    cfg = _cfg(monkeypatch)
    diagnostics = diagnostics_mod.build_provider_diagnostics(cfg)

    assert diagnostics.profile_name == "auth-review"
    assert any("auth_provider" in issue for issue in diagnostics.issues)
    assert diagnostics.cache_capability.source == "default"
    assert diagnostics.cache_policy.telemetry_metadata()["enabled"] is False
    # Reporting the issue does not convert an invalid auth route to API-key auth.
    with pytest.raises(ProviderAuthError, match="unregistered-review-adapter"):
        make_llm_client(cfg=cfg, api_key="", model=cfg.model)


def test_auth_diagnostic_does_not_publish_raw_constructor_error(monkeypatch) -> None:
    cfg = _cfg(monkeypatch)
    private_error = (
        "token=synthetic-private-value https://user:password@offline.invalid/?key=secret"
    )

    def failed_adapter(_provider_id):
        raise ProviderAuthError(private_error)

    monkeypatch.setattr(diagnostics_mod, "create_provider_auth", failed_adapter)
    diagnostics = diagnostics_mod.build_provider_diagnostics(cfg)
    rendered = str(diagnostics.rows())

    assert any("auth_provider" in issue for issue in diagnostics.issues)
    for private_value in ("synthetic-private-value", "password", "key=secret"):
        assert private_value not in rendered


def test_unexpected_auth_constructor_bug_is_not_silenced(monkeypatch) -> None:
    cfg = _cfg(monkeypatch)

    def broken_adapter(_provider_id):
        raise TypeError("synthetic programming error")

    monkeypatch.setattr(diagnostics_mod, "create_provider_auth", broken_adapter)
    with pytest.raises(TypeError, match="synthetic programming error"):
        diagnostics_mod.build_provider_diagnostics(cfg)
