from __future__ import annotations

import pytest

import alysis_code.model_registry as model_registry_mod
from alysis_code.config import AppConfig
from alysis_code.litellm_static_provider import (
    BUNDLED_MODEL_CATALOG_SOURCE,
    LiteLLMStaticMetadata,
)
from alysis_code.model_metadata_policy import (
    ActiveModelRef,
    ModelMetadataPolicyError,
    evaluate_active_model_metadata_policy,
)
from alysis_code.model_registry import ModelRegistry


def _bundled_meta(
    *,
    context_window_tokens: int | None,
    max_output_tokens: int | None,
    supports_vision: bool | None = None,
    input_cost_per_token: float | None = None,
    output_cost_per_token: float | None = None,
    error: str | None = None,
) -> LiteLLMStaticMetadata:
    return LiteLLMStaticMetadata(
        model_key="provider/test-model",
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        supports_vision=supports_vision,
        input_cost_per_token=input_cost_per_token,
        output_cost_per_token=output_cost_per_token,
        raw_metadata={},
        error=error,
    )


def test_evaluate_active_model_metadata_policy_warn_dedupes_same_model_roles() -> None:
    cfg = AppConfig(model="unknown-model-xyz")
    result = evaluate_active_model_metadata_policy(
        cfg=cfg,
        registry=ModelRegistry(cfg=cfg),
        active_models=[
            ActiveModelRef(role="coding", model_name="unknown-model-xyz"),
            ActiveModelRef(role="router", model_name="unknown-model-xyz"),
        ],
    )

    assert result.policy == "warn"
    assert len(result.diagnostics) == 2
    assert len(result.warning_messages) == 1
    warning = result.warning_messages[0]
    assert "Unknown model unknown-model-xyz" in warning
    assert "default context limits" in warning
    assert "/config set unknown-model-xyz <context> <max_output>" in warning
    assert "ALYSIS_CONTEXT_WINDOW" in warning
    assert "coding, router" not in warning
    assert "fallback capacity metadata" not in warning
    assert "Registry detail" not in warning
    assert all(diagnostic.fallback_capacity_active for diagnostic in result.diagnostics)


def test_model_registry_tolerates_malformed_base_url_during_metadata_lookup() -> None:
    cfg = AppConfig(model="unknown-model-xyz", base_url="https://api.deepseek.com]")

    result = evaluate_active_model_metadata_policy(
        cfg=cfg,
        registry=ModelRegistry(cfg=cfg),
        active_models=[ActiveModelRef(role="coding", model_name="unknown-model-xyz")],
    )

    assert result.policy == "warn"
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].fallback_capacity_active is True


def test_evaluate_active_model_metadata_policy_strict_rejects_fallback_capacity() -> None:
    cfg = AppConfig(model="unknown-model-xyz", model_metadata_policy="strict")

    with pytest.raises(ModelMetadataPolicyError, match="fallback capacity values are in use"):
        evaluate_active_model_metadata_policy(
            cfg=cfg,
            registry=ModelRegistry(cfg=cfg),
            active_models=[ActiveModelRef(role="coding", model_name="unknown-model-xyz")],
        )


def test_evaluate_active_model_metadata_policy_strict_allows_known_capacity_without_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None: _bundled_meta(
            context_window_tokens=128000,
            max_output_tokens=4096,
            supports_vision=None,
            input_cost_per_token=None,
            output_cost_per_token=None,
            error="pricing unavailable",
        ),
    )
    cfg = AppConfig(model="known-capacity-model", model_metadata_policy="strict")

    result = evaluate_active_model_metadata_policy(
        cfg=cfg,
        registry=ModelRegistry(cfg=cfg),
        active_models=[ActiveModelRef(role="coding", model_name="known-capacity-model")],
    )

    assert result.policy == "strict"
    assert result.warning_messages == ()
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.fallback_capacity_active is False
    assert diagnostic.field_sources["context_window_tokens"] == BUNDLED_MODEL_CATALOG_SOURCE
    assert diagnostic.field_sources["max_output_tokens"] == BUNDLED_MODEL_CATALOG_SOURCE


def test_evaluate_active_model_metadata_policy_warn_is_quiet_for_bundled_smoke_model() -> None:
    cfg = AppConfig(model="gpt-4o-mini")

    result = evaluate_active_model_metadata_policy(
        cfg=cfg,
        registry=ModelRegistry(cfg=cfg),
        active_models=[ActiveModelRef(role="coding", model_name="gpt-4o-mini")],
    )

    assert result.policy == "warn"
    assert result.warning_messages == ()
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.fallback_capacity_active is False
    assert diagnostic.field_sources["context_window_tokens"] == BUNDLED_MODEL_CATALOG_SOURCE
    assert diagnostic.field_sources["max_output_tokens"] == BUNDLED_MODEL_CATALOG_SOURCE
