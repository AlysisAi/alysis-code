from __future__ import annotations

from pathlib import Path

from alysis_code.config import AppConfig
from alysis_code.llm.cache_capabilities import (
    CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    CacheCapabilitySpec,
    resolve_effective_cache_capability,
)
from alysis_code.llm.cache_policy import (
    build_prompt_cache_namespace,
    derive_prompt_cache_stream_key,
    resolve_prompt_cache_policy,
)
from alysis_code.llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
    get_provider_protocol_capabilities,
)


def _capabilities(provider: str, protocol: str):
    capabilities = get_provider_protocol_capabilities(provider_key=provider, protocol=protocol)
    assert capabilities is not None
    return capabilities


def test_prompt_cache_stream_key_is_parent_scoped_and_shared_by_children() -> None:
    parent = derive_prompt_cache_stream_key(session_id="parent-session")
    first_child = derive_prompt_cache_stream_key(
        session_id="child-session-a",
        parent_session_id="parent-session",
    )
    second_child = derive_prompt_cache_stream_key(
        session_id="child-session-b",
        parent_session_id="parent-session",
    )

    assert parent == "parent-session"
    assert first_child == second_child
    assert first_child != parent
    assert "parent-session" not in first_child


def test_prompt_cache_key_toggle_disables_supported_and_unsupported_paths() -> None:
    disabled = AppConfig(cache={"prompt_cache_key_enabled": False})
    openai_policy = resolve_prompt_cache_policy(
        cfg=disabled,
        capabilities=_capabilities("openai", OPENAI_RESPONSES_PROTOCOL),
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model="gpt-test",
        prompt_cache_key="parent-session",
        prompt_cache_retention=None,
    )
    unsupported_policy = resolve_prompt_cache_policy(
        cfg=AppConfig(),
        capabilities=_capabilities("gemini", GEMINI_GENERATE_CONTENT_PROTOCOL),
        provider_key="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
        model="gemini-3-flash-preview",
        prompt_cache_key="parent-session",
        prompt_cache_retention=None,
    )

    assert openai_policy.prompt_cache_key is None
    assert "prompt_cache_key" not in openai_policy.emitted_fields
    assert unsupported_policy.prompt_cache_key is None
    assert "prompt_cache_key" not in unsupported_policy.emitted_fields


def test_auto_prompt_cache_policy_derives_hashed_openai_key(tmp_path: Path) -> None:
    namespace = build_prompt_cache_namespace(
        workspace_root=tmp_path / "repo",
        role="coding",
        profile_name="openai",
    )

    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="auto"),
        capabilities=_capabilities("openai", OPENAI_RESPONSES_PROTOCOL),
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model="gpt-test",
        prompt_cache_key=None,
        prompt_cache_retention=None,
        prompt_cache_namespace=namespace,
    )

    assert policy.prompt_cache_key is not None
    assert policy.prompt_cache_key.startswith("alysis:openai:")
    assert str(tmp_path) not in policy.prompt_cache_key
    assert policy.prompt_cache_retention is None
    assert policy.anthropic_cache_control_enabled is False
    assert policy.allowed_fields == ("prompt_cache_key", "prompt_cache_retention")
    assert policy.emitted_fields == ("prompt_cache_key",)
    assert policy.status == "enabled"


def test_auto_prompt_cache_key_is_role_scoped(tmp_path: Path) -> None:
    base_kwargs = {
        "cfg": AppConfig(prompt_cache_mode="auto"),
        "capabilities": _capabilities("openai", OPENAI_RESPONSES_PROTOCOL),
        "provider_key": "openai",
        "protocol": OPENAI_RESPONSES_PROTOCOL,
        "model": "gpt-test",
        "prompt_cache_key": None,
        "prompt_cache_retention": None,
    }

    coding = resolve_prompt_cache_policy(
        **base_kwargs,
        prompt_cache_namespace=build_prompt_cache_namespace(
            workspace_root=tmp_path,
            role="coding",
        ),
    )
    router = resolve_prompt_cache_policy(
        **base_kwargs,
        prompt_cache_namespace=build_prompt_cache_namespace(
            workspace_root=tmp_path,
            role="router",
        ),
    )

    assert coding.prompt_cache_key is not None
    assert router.prompt_cache_key is not None
    assert coding.prompt_cache_key != router.prompt_cache_key


def test_auto_prompt_cache_key_is_stable_per_session_and_changes_between_sessions(
    tmp_path: Path,
) -> None:
    common = dict(
        workspace_root=tmp_path,
        role="coding",
        profile_name="moonshot",
    )

    first = build_prompt_cache_namespace(**common, session_id="session-123")
    resumed = build_prompt_cache_namespace(**common, session_id="session-123")
    second = build_prompt_cache_namespace(**common, session_id="session-456")

    assert first == resumed
    assert first != second


def test_auto_prompt_cache_policy_requires_namespace_for_openai_auto_key() -> None:
    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="auto"),
        capabilities=_capabilities("openai", OPENAI_RESPONSES_PROTOCOL),
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model="gpt-test",
        prompt_cache_key=None,
        prompt_cache_retention=None,
        prompt_cache_namespace=None,
    )

    assert policy.prompt_cache_key is None
    assert policy.allowed_fields == ("prompt_cache_key", "prompt_cache_retention")
    assert policy.emitted_fields == ()
    assert policy.status == "available"


def test_manual_prompt_cache_policy_preserves_explicit_supported_fields() -> None:
    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="manual"),
        capabilities=_capabilities("openai", OPENAI_RESPONSES_PROTOCOL),
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model="gpt-test",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_namespace=None,
    )

    assert policy.prompt_cache_key == "repo-main"
    assert policy.prompt_cache_retention == "24h"
    assert policy.allowed_fields == ("prompt_cache_key", "prompt_cache_retention")
    assert policy.emitted_fields == ("prompt_cache_key", "prompt_cache_retention")
    assert policy.status == "enabled"
    assert policy.telemetry_metadata()["prompt_cache_key_hash"] == (
        "34d1041998a82cc916f8350e4adeecee7dde455059ee49f16fbcb7a153aefe1f"
    )


def test_prompt_cache_policy_uses_effective_profile_capability() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="custom",
        protocol="openai_compat",
        model="gateway-model",
        transport_capabilities=None,
        profile_cache_capability=CacheCapabilitySpec(
            strategy=CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
            enabled=True,
            supports_prompt_cache_key=True,
            reports_cache_read_tokens=True,
        ),
    )

    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="manual"),
        capabilities=None,
        cache_capability=capability,
        provider_key="custom",
        protocol="openai_compat",
        model="gateway-model",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_namespace=None,
    )

    assert policy.prompt_cache_key == "repo-main"
    assert policy.prompt_cache_retention is None
    assert policy.capability_source == "profile"
    assert policy.allowed_fields == ("prompt_cache_key",)
    assert policy.emitted_fields == ("prompt_cache_key",)
    assert policy.trusted_usage_fields == ("cache_read_input_tokens",)
    assert policy.status == "enabled"


def test_prompt_cache_policy_off_suppresses_request_cache_hints() -> None:
    cfg = AppConfig(
        prompt_cache_mode="off",
        anthropic_prompt_cache_enabled=True,
        anthropic_prompt_cache_ttl="1h",
    )

    policy = resolve_prompt_cache_policy(
        cfg=cfg,
        capabilities=_capabilities("anthropic", ANTHROPIC_MESSAGES_PROTOCOL),
        provider_key="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        model="claude-sonnet-4-6",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_namespace="namespace",
    )

    assert policy.prompt_cache_key is None
    assert policy.prompt_cache_retention is None
    assert policy.anthropic_cache_control_enabled is False
    assert policy.anthropic_cache_control_ttl == "1h"
    assert policy.allowed_fields == ("cache_control",)
    assert policy.emitted_fields == ()
    assert policy.status == "disabled"


def test_auto_prompt_cache_policy_enables_native_anthropic_cache_control() -> None:
    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="auto", anthropic_prompt_cache_ttl="1h"),
        capabilities=_capabilities("anthropic", ANTHROPIC_MESSAGES_PROTOCOL),
        provider_key="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        model="claude-sonnet-4-6",
        prompt_cache_key=None,
        prompt_cache_retention=None,
        prompt_cache_namespace="namespace",
    )

    assert policy.prompt_cache_key is None
    assert policy.prompt_cache_retention is None
    assert policy.anthropic_cache_control_enabled is True
    assert policy.anthropic_cache_control_ttl == "1h"
    assert policy.allowed_fields == ("cache_control",)
    assert policy.emitted_fields == ("cache_control",)
    assert policy.status == "enabled"


def test_auto_gemini_policy_converts_supported_retention_to_google_duration() -> None:
    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="auto"),
        capabilities=_capabilities("gemini", GEMINI_GENERATE_CONTENT_PROTOCOL),
        provider_key="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
        model="gemini-3-flash-preview",
        prompt_cache_key=None,
        prompt_cache_retention="24h",
        prompt_cache_namespace="namespace",
    )

    assert policy.gemini_explicit_cached_content_enabled is True
    assert policy.gemini_cached_content_ttl == "86400s"
    assert not any("unparseable_retention" in warning for warning in policy.warnings)


def test_auto_gemini_policy_falls_back_to_default_ttl_for_unparseable_retention() -> None:
    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="auto"),
        capabilities=_capabilities("gemini", GEMINI_GENERATE_CONTENT_PROTOCOL),
        provider_key="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
        model="gemini-3-flash-preview",
        prompt_cache_key=None,
        prompt_cache_retention="in-memory",
        prompt_cache_namespace="namespace",
    )

    assert policy.gemini_explicit_cached_content_enabled is True
    assert policy.gemini_cached_content_ttl == "3600s"
    assert "gemini_cached_content_ttl_fallback_3600s_for_unparseable_retention" in policy.warnings


def test_prompt_cache_policy_drops_fields_for_unsupported_native_gemini() -> None:
    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode="manual"),
        capabilities=_capabilities("gemini", GEMINI_GENERATE_CONTENT_PROTOCOL),
        provider_key="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
        model="gemini-3-flash-preview",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_namespace="namespace",
    )

    assert policy.prompt_cache_key is None
    assert policy.prompt_cache_retention is None
    assert policy.anthropic_cache_control_enabled is False
    assert policy.allowed_fields == ("cached_content",)
    assert policy.emitted_fields == ()
    assert policy.status == "available"
