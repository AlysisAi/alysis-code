from __future__ import annotations

from alysis_code.llm.cache_capabilities import (
    CACHE_CONTROL_FIELD,
    CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL,
    CACHE_STRATEGY_IMPLICIT_PROVIDER,
    CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
    CACHE_STRATEGY_NONE,
    CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
    CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
    OPENROUTER_SESSION_ID_FIELD,
    PROMPT_CACHE_KEY_FIELD,
    PROMPT_CACHE_RETENTION_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
    CacheCapabilitySpec,
    resolve_effective_cache_capability,
)
from alysis_code.llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
    get_provider_protocol_capabilities,
)
from alysis_code.profile_presets import get_preset


def test_unknown_openai_compatible_provider_is_safe_by_default() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="custom",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="custom-model",
        transport_capabilities=None,
    )

    assert capability.enabled is False
    assert capability.strategy == CACHE_STRATEGY_NONE
    assert capability.emitted_fields == ()


def test_profile_override_can_enable_safe_openai_compatible_projection() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="custom",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="custom-model",
        transport_capabilities=None,
        profile_cache_capability=CacheCapabilitySpec(
            strategy=CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
            enabled=True,
            supports_prompt_cache_key=True,
            reports_cache_read_tokens=True,
        ),
    )

    assert capability.enabled is True
    assert capability.strategy == CACHE_STRATEGY_OPENAI_PROMPT_CACHE
    assert capability.source == "profile"
    assert capability.emitted_fields == (PROMPT_CACHE_KEY_FIELD,)
    assert capability.trusted_usage_fields == ("cache_read_input_tokens",)


def test_endpoint_scoped_profile_cache_capability_applies_to_gateway_route() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="custom-gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen/qwen3-coder-plus",
        base_url="https://openrouter.ai/api/v1",
        transport_capabilities=None,
        profile_cache_capability={
            "endpoints": {
                "openrouter.ai": {
                    "strategy": CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
                    "enabled": True,
                    "emits_request_fields": True,
                    "request_fields": [OPENROUTER_SESSION_ID_FIELD],
                    "reports_cache_read_tokens": True,
                    "usage_schema": "provider",
                }
            }
        },
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_OPENROUTER_STICKY_SESSION
    assert capability.emitted_fields == (OPENROUTER_SESSION_ID_FIELD,)
    assert capability.trusted_usage_fields == ("cache_read_input_tokens",)


def test_model_family_and_exact_model_overrides_are_applied_in_precedence_order() -> None:
    profile_cache_capability = {
        "strategy": CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
        "enabled": True,
        "supports_prompt_cache_key": True,
        "emits_request_fields": True,
        "reports_cache_read_tokens": True,
        "model_families": {"qwen/": {"enabled": False}},
        "models": {
            "qwen/qwen3-coder-plus": {
                "strategy": CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
                "enabled": True,
                "supports_cache_control": True,
                "emits_request_fields": True,
                "reports_cache_read_tokens": True,
                "usage_schema": "provider",
            }
        },
    }

    family_disabled = resolve_effective_cache_capability(
        provider_key="custom-gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen/qwen3-max",
        transport_capabilities=None,
        profile_cache_capability=profile_cache_capability,
    )
    exact_enabled = resolve_effective_cache_capability(
        provider_key="custom-gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen/qwen3-coder-plus",
        transport_capabilities=None,
        profile_cache_capability=profile_cache_capability,
    )

    assert family_disabled.enabled is False
    assert family_disabled.strategy == CACHE_STRATEGY_NONE
    assert family_disabled.emitted_fields == ()
    assert exact_enabled.enabled is True
    assert exact_enabled.strategy == CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS
    assert exact_enabled.emitted_fields == (CACHE_CONTROL_FIELD,)


def test_model_scoped_profile_opt_in_emits_fields_without_explicit_emits_request_fields() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="custom-gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen/qwen3-coder-plus",
        transport_capabilities=None,
        profile_cache_capability={
            "models": {
                "qwen/qwen3-coder-plus": {
                    "strategy": CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
                    "enabled": True,
                    "supports_cache_control": True,
                    "reports_cache_read_tokens": True,
                    "usage_schema": "provider",
                }
            }
        },
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS
    assert capability.emits_request_fields is True
    assert capability.emitted_fields == (CACHE_CONTROL_FIELD,)
    assert capability.trusted_usage_fields == ("cache_read_input_tokens",)


def test_endpoint_scoped_profile_opt_in_emits_fields_without_explicit_emits_request_fields() -> (
    None
):
    capability = resolve_effective_cache_capability(
        provider_key="custom-gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen/qwen3-coder-plus",
        base_url="https://openrouter.ai/api/v1",
        transport_capabilities=None,
        profile_cache_capability={
            "endpoints": {
                "openrouter.ai": {
                    "strategy": CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
                    "enabled": True,
                    "request_fields": [OPENROUTER_SESSION_ID_FIELD],
                    "reports_cache_read_tokens": True,
                    "usage_schema": "provider",
                }
            }
        },
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_OPENROUTER_STICKY_SESSION
    assert capability.emits_request_fields is True
    assert capability.emitted_fields == (OPENROUTER_SESSION_ID_FIELD,)


def test_model_scoped_preset_opt_in_stays_diagnostic_without_explicit_emits_request_fields() -> (
    None
):
    capability = resolve_effective_cache_capability(
        provider_key="custom-gateway",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen/qwen3-coder-plus",
        transport_capabilities=None,
        preset_cache_capability={
            "models": {
                "qwen/qwen3-coder-plus": {
                    "strategy": CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
                    "enabled": True,
                    "supports_cache_control": True,
                    "reports_cache_read_tokens": True,
                    "usage_schema": "provider",
                }
            }
        },
    )

    assert capability.enabled is True
    assert capability.status == "available"
    assert capability.emits_request_fields is False
    assert capability.emitted_fields == ()


def test_runtime_disabled_fields_create_session_local_capability_downgrade() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="custom",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="custom-model",
        transport_capabilities=None,
        profile_cache_capability=CacheCapabilitySpec(
            strategy=CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
            enabled=True,
            supports_prompt_cache_key=True,
            supports_prompt_cache_retention=True,
            reports_cache_read_tokens=True,
        ),
        runtime_disabled_fields=(PROMPT_CACHE_RETENTION_FIELD,),
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.emitted_fields == (PROMPT_CACHE_KEY_FIELD,)
    assert any("downgrade" in warning for warning in capability.warnings)


def test_profile_override_cannot_project_fields_the_transport_cannot_send() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model="gpt-test",
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key="openai",
            protocol=OPENAI_RESPONSES_PROTOCOL,
        ),
        profile_cache_capability=CacheCapabilitySpec(
            strategy=CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL,
            enabled=True,
            supports_cache_control=True,
        ),
    )

    assert capability.enabled is False
    assert capability.emitted_fields == ()
    assert any(CACHE_CONTROL_FIELD in warning for warning in capability.warnings)


def test_profile_override_can_disable_builtin_capability() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        model="claude-test",
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        ),
        profile_cache_capability=CacheCapabilitySpec(enabled=False),
    )

    assert capability.enabled is False
    assert capability.strategy == CACHE_STRATEGY_NONE
    assert capability.source == "profile"
    assert capability.emitted_fields == ()


def test_mistral_preset_emits_prompt_cache_key_only() -> None:
    preset = get_preset("mistral")
    assert preset is not None

    capability = resolve_effective_cache_capability(
        provider_key="mistral",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="mistral-medium-3-5",
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key="mistral",
            protocol=OPENAI_COMPAT_PROTOCOL,
        ),
        preset_cache_capability=preset.cache_capability,
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY
    assert capability.source == "preset"
    assert capability.emits_request_fields is True
    assert capability.emitted_fields == (PROMPT_CACHE_KEY_FIELD,)
    assert capability.trusted_usage_fields == ("cache_read_input_tokens",)


def test_moonshot_preset_emits_cache_affinity_key_and_trusts_read_usage() -> None:
    preset = get_preset("moonshot")
    assert preset is not None

    capability = resolve_effective_cache_capability(
        provider_key="moonshot",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="kimi-k3",
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key="moonshot",
            protocol=OPENAI_COMPAT_PROTOCOL,
        ),
        preset_cache_capability=preset.cache_capability,
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_IMPLICIT_PROVIDER
    assert capability.emitted_fields == (PROMPT_CACHE_KEY_FIELD,)
    assert capability.trusted_usage_fields == ("cache_read_input_tokens",)


def test_openrouter_preset_emits_sticky_session_body_field() -> None:
    preset = get_preset("openrouter")
    assert preset is not None

    capability = resolve_effective_cache_capability(
        provider_key="openrouter",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen/qwen3.7-plus",
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key="openrouter",
            protocol=OPENAI_COMPAT_PROTOCOL,
        ),
        preset_cache_capability=preset.cache_capability,
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_OPENROUTER_STICKY_SESSION
    assert capability.emits_request_fields is True
    assert capability.emitted_fields == (OPENROUTER_SESSION_ID_FIELD,)
    assert "cache_read_input_tokens" in capability.trusted_usage_fields


def test_profile_override_can_project_openai_compatible_cache_control_blocks() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="qwen",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen-coder",
        transport_capabilities=None,
        profile_cache_capability=CacheCapabilitySpec(
            strategy=CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
            enabled=True,
            supports_cache_control=True,
            reports_cache_read_tokens=True,
            min_cacheable_tokens=1,
        ),
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS
    assert capability.emitted_fields == (CACHE_CONTROL_FIELD,)
    assert capability.min_cacheable_tokens == 1


def test_qwen_builtin_preset_stays_diagnostic_until_explicit_opt_in() -> None:
    preset = get_preset("qwen-intl")
    assert preset is not None

    capability = resolve_effective_cache_capability(
        provider_key="qwen",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="qwen3-coder-plus",
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key="qwen",
            protocol=OPENAI_COMPAT_PROTOCOL,
        ),
        preset_cache_capability=preset.cache_capability,
    )

    assert capability.enabled is True
    assert capability.status == "available"
    assert capability.strategy == CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS
    assert capability.emitted_fields == ()


def test_xai_preset_emits_conversation_header_field() -> None:
    preset = get_preset("xai")
    assert preset is not None

    capability = resolve_effective_cache_capability(
        provider_key="xai",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model="grok-4.3",
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key="xai",
            protocol=OPENAI_COMPAT_PROTOCOL,
        ),
        preset_cache_capability=preset.cache_capability,
    )

    assert capability.enabled is True
    assert capability.status == "enabled"
    assert capability.strategy == CACHE_STRATEGY_XAI_CONVERSATION_HEADER
    assert capability.emitted_fields == (XAI_CONVERSATION_ID_HEADER_FIELD,)
