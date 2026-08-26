from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .llm.cache_capabilities import (
    CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL,
    CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT,
    CACHE_STRATEGY_IMPLICIT_PROVIDER,
    CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
    CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
    CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
    CACHE_USAGE_SCHEMA_ANTHROPIC,
    CACHE_USAGE_SCHEMA_GEMINI,
    CACHE_USAGE_SCHEMA_OPENAI,
    CACHE_USAGE_SCHEMA_PROVIDER,
    OPENROUTER_SESSION_ID_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
    CacheCapabilitySpec,
)
from .llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
)
from .profiles import ProfileSpec
from .web_search_adapters import (
    ANTHROPIC_MESSAGES_ADAPTER,
    AUTO_WEB_SEARCH_ADAPTER,
    COHERE_WEB_SEARCH_ADAPTER,
    DASHSCOPE_CHAT_ADAPTER,
    GEMINI_GROUNDING_ADAPTER,
    GROQ_COMPOUND_ADAPTER,
    MINIMAX_CODING_PLAN_ADAPTER,
    MISTRAL_CONVERSATIONS_ADAPTER,
    MOONSHOT_KIMI_ADAPTER,
    OPENAI_RESPONSES_ADAPTER,
    OPENROUTER_WEB_ADAPTER,
    PERPLEXITY_SONAR_ADAPTER,
    VOLCENGINE_WEB_SEARCH_ADAPTER,
    XAI_RESPONSES_ADAPTER,
    ZHIPU_WEB_SEARCH_ADAPTER,
)

NATIVE_PROFILE_PROTOCOLS: frozenset[str] = frozenset(
    {
        OPENAI_RESPONSES_PROTOCOL,
        ANTHROPIC_MESSAGES_PROTOCOL,
        GEMINI_GENERATE_CONTENT_PROTOCOL,
        GEMINI_INTERACTIONS_PROTOCOL,
    }
)
FIRST_PARTY_NATIVE_PRESET_KEYS: tuple[str, ...] = (
    "openai-responses",
    "anthropic",
    "gemini",
)
FIRST_CLASS_SETUP_PRESET_KEYS: tuple[str, ...] = (
    # "alysis" (hosted MiMo) deliberately absent: while no campaign is
    # running it stays off the primary picker entirely (advanced picker only).
    *FIRST_PARTY_NATIVE_PRESET_KEYS,
)
FIRST_PARTY_COMPATIBILITY_PRESET_KEYS: tuple[str, ...] = (
    "openai",
    "anthropic-compat",
    "gemini-compat",
)
LEGACY_NATIVE_ALIAS_PRESET_KEYS: tuple[str, ...] = ("anthropic-native", "gemini-native")
LOCAL_PROFILE_PRESET_KEYS: tuple[str, ...] = ("ollama", "lm-studio", "vllm")
_CUSTOM_PRESET_KEY = "custom"
# Account-gated hosted presets (`alysis login`, no API key). Kept off the
# primary provider picker while no hosted campaign is running.
_ACCOUNT_GATED_PRESET_KEYS: tuple[str, ...] = ("alysis",)
_CONVERSION_PRESET_BY_FAMILY: dict[str, dict[str, str]] = {
    "openai": {"native": "openai-responses", "compatibility": "openai"},
    "anthropic": {"native": "anthropic", "compatibility": "anthropic-compat"},
    "gemini": {"native": "gemini", "compatibility": "gemini-compat"},
}


@dataclass(frozen=True)
class ProfilePreset:
    key: str
    label: str
    protocol: str
    base_url: str
    api_key_env: str | None
    extra_headers: dict[str, str] = field(default_factory=dict)
    suggested_models: tuple[str, ...] = ()
    suggested_model_descriptions: dict[str, str] = field(default_factory=dict)
    model_aliases: dict[str, str] = field(default_factory=dict)
    validation_model: str = ""
    web_search_adapter: str = AUTO_WEB_SEARCH_ADAPTER
    web_search_model: str = ""
    setup_warning: str = ""
    notes: str = ""
    cache_capability: CacheCapabilitySpec | None = None
    # Keep new optional fields at the end so extensions using the legacy
    # positional constructor continue to bind the sixth argument to headers.
    provider_key: str = ""


_OPENAI_PROMPT_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    enabled=True,
    supports_prompt_cache_key=True,
    supports_prompt_cache_retention=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_OPENAI,
    min_cacheable_tokens=1024,
    source="preset",
)


def _cache_minimum(tokens: int) -> CacheCapabilitySpec:
    """A scoped override that only narrows the minimum cacheable prefix."""

    return CacheCapabilitySpec(min_cacheable_tokens=tokens, source="preset")


# Anthropic's minimum cacheable prefix is per-model and does not move
# monotonically with the version number: the 5-generation flagships halved the
# floor to 512 while Opus 4.6/4.5 and Haiku 4.5 still need 4096. A prefix under
# the floor is silently not cached, so the write premium buys nothing — the
# floor is what makes the request-shape report say so instead of guessing.
_ANTHROPIC_CACHE_CONTROL_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL,
    enabled=True,
    supports_cache_control=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_ANTHROPIC,
    min_cacheable_tokens=1024,
    model_family_overrides=(
        ("claude-opus-5", _cache_minimum(512)),
        ("claude-fable-5", _cache_minimum(512)),
        ("claude-mythos-5", _cache_minimum(512)),
        ("claude-mythos-preview", _cache_minimum(2048)),
        ("claude-opus-4-7", _cache_minimum(2048)),
        ("claude-opus-4-6", _cache_minimum(4096)),
        ("claude-opus-4-5", _cache_minimum(4096)),
        ("claude-haiku-4-5", _cache_minimum(4096)),
    ),
    source="preset",
)
_GEMINI_EXPLICIT_CACHED_CONTENT_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT,
    enabled=True,
    supports_explicit_cached_content=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_GEMINI,
    min_cacheable_tokens=4096,
    source="preset",
)
_MISTRAL_PROMPT_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
    enabled=True,
    supports_prompt_cache_key=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_OPENAI,
    min_cacheable_tokens=1024,
    emits_request_fields=True,
    notes=("Emits Mistral prompt_cache_key for stable server routing and prompt-cache hits.",),
    source="preset",
)
_OPENROUTER_STICKY_SESSION_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
    enabled=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=True,
    request_fields=(OPENROUTER_SESSION_ID_FIELD,),
    notes=(
        "Emits OpenRouter session_id for sticky routing; upstream cache semantics remain "
        "route-dependent.",
    ),
    source="preset",
)
_XAI_CONVERSATION_HEADER_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
    enabled=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=True,
    request_fields=(XAI_CONVERSATION_ID_HEADER_FIELD,),
    notes=("Emits x-grok-conv-id for sticky cache routing on xAI Chat Completions.",),
    source="preset",
)
_QWEN_DIAGNOSTIC_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    enabled=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    min_cacheable_tokens=1024,
    emits_request_fields=False,
    notes=(
        "Diagnostic-only in auto mode; Qwen cache_control content markers mutate "
        "message shape and require request-shape gating.",
    ),
    source="preset",
)
_MOONSHOT_AUTOMATIC_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_IMPLICIT_PROVIDER,
    enabled=True,
    supports_prompt_cache_key=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=True,
    notes=(
        "Moonshot caches matching prompt prefixes automatically; prompt_cache_key keeps a "
        "session on a stable cache-affinity route.",
    ),
    source="preset",
)
_ZAI_CODING_PLAN_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_IMPLICIT_PROVIDER,
    enabled=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=False,
    notes=(
        "Z.AI Coding Plan caches matching prompt prefixes automatically and reports "
        "cached-input usage; Alysis Code emits no provider-specific cache fields.",
    ),
    source="preset",
)


def preset_protocol_kind(preset: ProfilePreset) -> str:
    return "native" if preset.protocol in NATIVE_PROFILE_PROTOCOLS else "compatibility"


def preset_protocol_summary(preset: ProfilePreset) -> str:
    if preset.protocol in NATIVE_PROFILE_PROTOCOLS:
        return (
            f"Native first-party protocol: {preset.protocol} (recommended for first-party API keys)"
        )
    return "Compatibility protocol: OpenAI-compatible chat transport"


def preset_selection_label(preset: ProfilePreset) -> str:
    """Return a setup/config label that keeps protocol details out of the primary choice."""
    if preset.key == "alysis":
        return "Alysis Code Pro (hosted models) - requires login"
    if preset.key == "openai-responses":
        return "OpenAI - Native Responses"
    if preset.key in {"anthropic", "anthropic-native"}:
        return "Anthropic Claude - Native Messages"
    if preset.key in {"gemini", "gemini-native"}:
        return "Google Gemini - Native GenerateContent"
    if preset.key == "openai":
        return "OpenAI - Compatibility/gateway Chat Completions"
    if preset.key == "anthropic-compat":
        return "Anthropic Claude compatibility - legacy OpenAI-compatible"
    if preset.key == "gemini-compat":
        return "Google Gemini compatibility - legacy OpenAI-compatible"
    if preset.key in LOCAL_PROFILE_PRESET_KEYS:
        return f"{preset.label} - Local endpoint"
    if preset.key == "custom":
        return "Custom OpenAI-compatible endpoint"
    return preset.label


def _advanced_only_preset_keys() -> frozenset[str]:
    """Preset keys deliberately kept off the primary provider picker.

    Everything else in :data:`PROFILE_PRESETS` is a real hosted provider — the
    native first-party APIs *and* the third-party API/gateway endpoints — and is
    surfaced directly so users are not limited to the big-three brands. Only the
    OpenAI-compatible duplicates of the native first-party providers, local
    endpoints (Ollama/LM Studio/vLLM), the manual custom-URL entry, the
    one-release legacy aliases, and the account-gated hosted MiMo preset
    (no hosted campaign is running, so it is not a provider choice) stay
    behind the advanced picker.
    """
    return frozenset(
        {
            _CUSTOM_PRESET_KEY,
            *FIRST_PARTY_COMPATIBILITY_PRESET_KEYS,
            *LOCAL_PROFILE_PRESET_KEYS,
            *LEGACY_NATIVE_ALIAS_PRESET_KEYS,
            *_ACCOUNT_GATED_PRESET_KEYS,
        }
    )


def provider_selection_presets() -> list[ProfilePreset]:
    """Presets shown directly on the primary provider picker.

    Native first-party providers lead — the best defaults for new users —
    followed by every other hosted provider in registration order.
    Compatibility duplicates, local endpoints, the custom-URL entry,
    one-release legacy aliases, and the account-gated hosted MiMo preset are
    the only presets held back for the advanced picker, so the user sees the
    full range of hosted providers up front instead of just
    OpenAI/Anthropic/Gemini.
    """
    by_key = PRESET_BY_KEY
    advanced = _advanced_only_preset_keys()
    leading = [by_key[key] for key in FIRST_CLASS_SETUP_PRESET_KEYS if key in by_key]
    leading_keys = {preset.key for preset in leading}
    rest = [
        preset
        for preset in PROFILE_PRESETS
        if preset.key not in advanced and preset.key not in leading_keys
    ]
    return [*leading, *rest]


def advanced_provider_selection_presets() -> list[ProfilePreset]:
    """Return the compatibility, local, custom, legacy alias, and account-gated presets.

    These are exactly the presets held off the primary provider picker: the
    OpenAI-compatible duplicates of the native first-party providers, local
    endpoints (Ollama/LM Studio/vLLM), the manual custom-URL entry, the
    one-release legacy aliases, and the account-gated hosted MiMo preset.
    """
    by_key = PRESET_BY_KEY
    first_party_compat = [
        by_key[key] for key in FIRST_PARTY_COMPATIBILITY_PRESET_KEYS if key in by_key
    ]
    local = [by_key[key] for key in LOCAL_PROFILE_PRESET_KEYS if key in by_key]
    custom = [by_key[_CUSTOM_PRESET_KEY]] if _CUSTOM_PRESET_KEY in by_key else []
    aliases = [by_key[key] for key in LEGACY_NATIVE_ALIAS_PRESET_KEYS if key in by_key]
    account_gated = [by_key[key] for key in _ACCOUNT_GATED_PRESET_KEYS if key in by_key]
    return [*first_party_compat, *local, *custom, *aliases, *account_gated]


PROFILE_PRESETS: tuple[ProfilePreset, ...] = (
    ProfilePreset(
        key="openai",
        provider_key="openai",
        label="OpenAI",
        protocol="openai_compat",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        suggested_models=(
            "gpt-5.6-terra",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "gpt-5.3-codex",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
        ),
        suggested_model_descriptions={
            "gpt-5.6-terra": "default - balanced 5.6 tier, 1.05M context",
            "gpt-5.6-sol": "advanced - flagship 5.6 tier, 1.05M context",
            "gpt-5.6-luna": "fast - low-cost 5.6 tier, full 1.05M context",
            "gpt-5.3-codex": "coding - agentic codex model, 400K context",
            "gpt-5.4-mini": "fallback - cheap tier for subagents, 400K",
            "gpt-5.4-nano": "economy - cheapest live id, 400K context",
        },
        model_aliases={
            "gpt-5.6": "gpt-5.6-sol",
            "gpt-5-nano": "gpt-5.4-nano",
            # 2026-07-23 shutdowns from OpenAI's deprecations page: codex and
            # chat-latest ids remap to the still-callable gpt-5.5 tier.
            "gpt-5-codex": "gpt-5.5",
            "gpt-5.1-codex": "gpt-5.5",
            "gpt-5.1-codex-max": "gpt-5.5",
            "gpt-5.2-codex": "gpt-5.5",
            "gpt-5.1-codex-mini": "gpt-5.4-mini",
            "gpt-5-chat-latest": "gpt-5.5",
            "gpt-5.1-chat-latest": "gpt-5.5",
        },
        validation_model="gpt-5.4-nano",
        web_search_adapter=OPENAI_RESPONSES_ADAPTER,
        cache_capability=_OPENAI_PROMPT_CACHE_CAPABILITY,
        setup_warning=(
            "gpt-5.6/5.4 reject tool calls with reasoning_effort other than "
            "'none' on Chat Completions (and 5.6 defaults to 'medium') — for "
            "agentic runs use the OpenAI Responses preset, or pin effort to "
            "'none' here."
        ),
    ),
    ProfilePreset(
        key="openai-responses",
        provider_key="openai",
        label="OpenAI Responses",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        suggested_models=(
            "gpt-5.6-terra",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "gpt-5.3-codex",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
        ),
        suggested_model_descriptions={
            "gpt-5.6-terra": "default - balanced 5.6 tier, 1.05M context",
            "gpt-5.6-sol": "advanced - flagship 5.6 tier, 1.05M context",
            "gpt-5.6-luna": "fast - low-cost 5.6 tier, full 1.05M context",
            "gpt-5.3-codex": "coding - agentic codex model, 400K context",
            "gpt-5.4-mini": "fallback - cheap tier for subagents, 400K",
            "gpt-5.4-nano": "economy - cheapest live id, 400K context",
        },
        model_aliases={
            "gpt-5.6": "gpt-5.6-sol",
            "gpt-5-nano": "gpt-5.4-nano",
            # 2026-07-23 shutdowns from OpenAI's deprecations page: codex and
            # chat-latest ids remap to the still-callable gpt-5.5 tier.
            "gpt-5-codex": "gpt-5.5",
            "gpt-5.1-codex": "gpt-5.5",
            "gpt-5.1-codex-max": "gpt-5.5",
            "gpt-5.2-codex": "gpt-5.5",
            "gpt-5.1-codex-mini": "gpt-5.4-mini",
            "gpt-5-chat-latest": "gpt-5.5",
            "gpt-5.1-chat-latest": "gpt-5.5",
        },
        validation_model="gpt-5.4-nano",
        web_search_adapter=OPENAI_RESPONSES_ADAPTER,
        cache_capability=_OPENAI_PROMPT_CACHE_CAPABILITY,
        notes=(
            "Native OpenAI Responses API chat with SSE streaming support. Use the OpenAI compat "
            "preset to keep Chat Completions-compatible behavior."
        ),
    ),
    ProfilePreset(
        key="anthropic",
        provider_key="anthropic",
        label="Anthropic Claude",
        protocol="anthropic_messages",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        suggested_models=(
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-fable-5",
            "claude-haiku-4-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
        ),
        suggested_model_descriptions={
            "claude-sonnet-5": "default - 1M context, best speed/intelligence mix",
            "claude-opus-5": "advanced - agentic coding + deep reasoning, 1M ctx",
            "claude-fable-5": "reasoning - adaptive thinking always on, 1M ctx",
            "claude-haiku-4-5": "fast - 200K context, lowest cost tier",
            "claude-opus-4-8": "fallback - previous-generation opus, 1M context",
            "claude-opus-4-7": "legacy - prior opus generation, 1M context",
        },
        model_aliases={
            # claude-sonnet-4-6 moved to Anthropic's Legacy table; Sonnet 5 is
            # newer and cheaper. Retired haiku ids remap to the 4.5 bare alias.
            "claude-sonnet-4": "claude-sonnet-5",
            "claude-sonnet-4-5": "claude-sonnet-5",
            "claude-sonnet-4-6": "claude-sonnet-5",
            "claude-4-sonnet": "claude-sonnet-5",
            "claude-3-5-haiku-latest": "claude-haiku-4-5",
            "claude-3-5-haiku-20241022": "claude-haiku-4-5",
            "claude-opus-4.8": "claude-opus-4-8",
            "claude-opus-4.7": "claude-opus-4-7",
            "claude-opus-4-1": "claude-opus-4-8",
            "claude-opus-4-6": "claude-opus-4-8",
        },
        validation_model="claude-haiku-4-5",
        web_search_adapter=ANTHROPIC_MESSAGES_ADAPTER,
        cache_capability=_ANTHROPIC_CACHE_CONTROL_CAPABILITY,
        notes=(
            "Native Anthropic Messages API chat with SSE streaming support. Compatibility mode "
            "remains available as anthropic-compat for legacy OpenAI-compatible fallback."
        ),
    ),
    ProfilePreset(
        key="anthropic-compat",
        provider_key="anthropic",
        label="Anthropic Claude compatibility",
        protocol="openai_compat",
        base_url="https://api.anthropic.com/v1/",
        api_key_env="ANTHROPIC_API_KEY",
        suggested_models=(
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-fable-5",
            "claude-haiku-4-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
        ),
        suggested_model_descriptions={
            "claude-sonnet-5": "default - 1M context, best speed/intelligence mix",
            "claude-opus-5": "advanced - agentic coding + deep reasoning, 1M ctx",
            "claude-fable-5": "reasoning - adaptive thinking always on, 1M ctx",
            "claude-haiku-4-5": "fast - 200K context, lowest cost tier",
            "claude-opus-4-8": "fallback - previous-generation opus, 1M context",
            "claude-opus-4-7": "legacy - prior opus generation, 1M context",
        },
        model_aliases={
            # claude-sonnet-4-6 moved to Anthropic's Legacy table; Sonnet 5 is
            # newer and cheaper. Retired haiku ids remap to the 4.5 bare alias.
            "claude-sonnet-4": "claude-sonnet-5",
            "claude-sonnet-4-5": "claude-sonnet-5",
            "claude-sonnet-4-6": "claude-sonnet-5",
            "claude-4-sonnet": "claude-sonnet-5",
            "claude-3-5-haiku-latest": "claude-haiku-4-5",
            "claude-3-5-haiku-20241022": "claude-haiku-4-5",
            "claude-opus-4.8": "claude-opus-4-8",
            "claude-opus-4.7": "claude-opus-4-7",
            "claude-opus-4-1": "claude-opus-4-8",
            "claude-opus-4-6": "claude-opus-4-8",
        },
        validation_model="claude-haiku-4-5",
        web_search_adapter=ANTHROPIC_MESSAGES_ADAPTER,
        setup_warning=(
            "Anthropic labels the OpenAI SDK compatibility layer as a test path; "
            "use the anthropic preset for native Messages API behavior."
        ),
        notes=(
            "Chat uses Anthropic OpenAI-compat at /v1; web_search uses the native "
            "Anthropic Messages web_search adapter when the model/account supports it."
        ),
    ),
    ProfilePreset(
        key="anthropic-native",
        provider_key="anthropic",
        label="Anthropic Claude (native alias)",
        protocol="anthropic_messages",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        suggested_models=(
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-fable-5",
            "claude-haiku-4-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
        ),
        suggested_model_descriptions={
            "claude-sonnet-5": "default - 1M context, best speed/intelligence mix",
            "claude-opus-5": "advanced - agentic coding + deep reasoning, 1M ctx",
            "claude-fable-5": "reasoning - adaptive thinking always on, 1M ctx",
            "claude-haiku-4-5": "fast - 200K context, lowest cost tier",
            "claude-opus-4-8": "fallback - previous-generation opus, 1M context",
            "claude-opus-4-7": "legacy - prior opus generation, 1M context",
        },
        model_aliases={
            # claude-sonnet-4-6 moved to Anthropic's Legacy table; Sonnet 5 is
            # newer and cheaper. Retired haiku ids remap to the 4.5 bare alias.
            "claude-sonnet-4": "claude-sonnet-5",
            "claude-sonnet-4-5": "claude-sonnet-5",
            "claude-sonnet-4-6": "claude-sonnet-5",
            "claude-4-sonnet": "claude-sonnet-5",
            "claude-3-5-haiku-latest": "claude-haiku-4-5",
            "claude-3-5-haiku-20241022": "claude-haiku-4-5",
            "claude-opus-4.8": "claude-opus-4-8",
            "claude-opus-4.7": "claude-opus-4-7",
            "claude-opus-4-1": "claude-opus-4-8",
            "claude-opus-4-6": "claude-opus-4-8",
        },
        validation_model="claude-haiku-4-5",
        web_search_adapter=ANTHROPIC_MESSAGES_ADAPTER,
        cache_capability=_ANTHROPIC_CACHE_CONTROL_CAPABILITY,
        notes=(
            "Legacy alias for the native anthropic preset. Prefer the anthropic preset for new "
            "first-party Claude profiles."
        ),
    ),
    ProfilePreset(
        key="gemini",
        provider_key="gemini",
        label="Google Gemini",
        protocol="gemini_generate_content",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
        suggested_models=(
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-pro-preview",
        ),
        suggested_model_descriptions={
            "gemini-3.7-flash": "default - newest GA coding and agentic model, 1M",
            "gemini-3.6-flash": "fallback - production GA flash model, 1M context",
            "gemini-3.5-flash-lite": "economy - lowest-cost GA tier, 1M context",
            "gemini-3.1-pro-preview": "advanced - pro reasoning preview, 1M context",
        },
        model_aliases={
            # Only shut-down or invalid legacy ids are rewritten. Active stable
            # ids and provider-managed *-latest aliases pass through unchanged.
            "gemini-2.0-flash": "gemini-3.6-flash",
            "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
            "gemini-3.1-preview": "gemini-3.1-pro-preview",
            "gemini-3-pro-preview": "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
        },
        validation_model="gemini-3.5-flash-lite",
        web_search_adapter=GEMINI_GROUNDING_ADAPTER,
        cache_capability=_GEMINI_EXPLICIT_CACHED_CONTENT_CAPABILITY,
        setup_warning=(
            "Gemini native GenerateContent uses the Google Gemini API v1beta surface and "
            "model availability can vary by account, region, and provider rollout."
        ),
        notes=(
            "Native Gemini GenerateContent API chat with streamGenerateContent SSE support. "
            "Compatibility mode remains available as gemini-compat for legacy OpenAI-compatible "
            "fallback."
        ),
    ),
    ProfilePreset(
        key="gemini-compat",
        provider_key="gemini",
        label="Google Gemini compatibility",
        protocol="openai_compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        suggested_models=(
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-pro-preview",
        ),
        suggested_model_descriptions={
            "gemini-3.7-flash": "default - newest GA coding and agentic model, 1M",
            "gemini-3.6-flash": "fallback - production GA flash model, 1M context",
            "gemini-3.5-flash-lite": "economy - lowest-cost GA tier, 1M context",
            "gemini-3.1-pro-preview": "advanced - pro reasoning preview, 1M context",
        },
        model_aliases={
            # Only shut-down or invalid legacy ids are rewritten. Active stable
            # ids and provider-managed *-latest aliases pass through unchanged.
            "gemini-2.0-flash": "gemini-3.6-flash",
            "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
            "gemini-3.1-preview": "gemini-3.1-pro-preview",
            "gemini-3-pro-preview": "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
        },
        validation_model="gemini-3.5-flash-lite",
        web_search_adapter=GEMINI_GROUNDING_ADAPTER,
        setup_warning=(
            "Gemini OpenAI compatibility is served from v1beta; use the gemini preset for "
            "native GenerateContent behavior."
        ),
    ),
    ProfilePreset(
        key="gemini-native",
        provider_key="gemini",
        label="Google Gemini (native alias)",
        protocol="gemini_generate_content",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
        suggested_models=(
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-pro-preview",
        ),
        suggested_model_descriptions={
            "gemini-3.7-flash": "default - newest GA coding and agentic model, 1M",
            "gemini-3.6-flash": "fallback - production GA flash model, 1M context",
            "gemini-3.5-flash-lite": "economy - lowest-cost GA tier, 1M context",
            "gemini-3.1-pro-preview": "advanced - pro reasoning preview, 1M context",
        },
        model_aliases={
            # Only shut-down or invalid legacy ids are rewritten. Active stable
            # ids and provider-managed *-latest aliases pass through unchanged.
            "gemini-2.0-flash": "gemini-3.6-flash",
            "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
            "gemini-3.1-preview": "gemini-3.1-pro-preview",
            "gemini-3-pro-preview": "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
        },
        validation_model="gemini-3.5-flash-lite",
        web_search_adapter=GEMINI_GROUNDING_ADAPTER,
        cache_capability=_GEMINI_EXPLICIT_CACHED_CONTENT_CAPABILITY,
        setup_warning=(
            "Gemini native GenerateContent uses the Google Gemini API v1beta surface and "
            "model availability can vary by account, region, and provider rollout."
        ),
        notes=(
            "Legacy alias for the native gemini preset. Prefer the gemini preset for new "
            "first-party Gemini profiles."
        ),
    ),
    ProfilePreset(
        key="deepseek",
        provider_key="deepseek",
        label="DeepSeek",
        protocol="openai_compat",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        suggested_models=(
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        ),
        suggested_model_descriptions={
            "deepseek-v4-pro": "default - flagship coding model, 1M context",
            "deepseek-v4-flash": "fast - cheap high-volume work, 1M context",
            "deepseek-v4-flash-vision-exp": (
                "vision preview - image understanding and tools, 1M context"
            ),
        },
        model_aliases={
            # deepseek-chat / deepseek-reasoner are discontinued 2026-07-24;
            # saved configs pinning them keep working via these remaps.
            "deepseek-chat": "deepseek-v4-flash",
            "deepseek-reasoner": "deepseek-v4-flash",
        },
        validation_model="deepseek-v4-flash",
        setup_warning=(
            "Do not use retired legacy aliases deepseek-chat or deepseek-reasoner "
            "for production defaults; use the V4 model IDs. The vision model is "
            "experimental and may change without a stable-release deprecation window."
        ),
    ),
    ProfilePreset(
        key="nvidia",
        provider_key="nvidia",
        label="NVIDIA NIM (Hosted)",
        protocol="openai_compat",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        suggested_models=(
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia/nemotron-3-nano-30b-a3b",
            "deepseek-ai/deepseek-v4-pro",
            "deepseek-ai/deepseek-v4-flash",
        ),
        suggested_model_descriptions={
            "nvidia/nemotron-3-super-120b-a12b": (
                "default - balanced agentic reasoning, 1M context"
            ),
            "nvidia/nemotron-3-ultra-550b-a55b": (
                "advanced - frontier agentic reasoning, 1M context"
            ),
            "nvidia/nemotron-3-nano-30b-a3b": (
                "fast - efficient reasoning and tool use, 262K hosted context"
            ),
            "deepseek-ai/deepseek-v4-pro": (
                "third-party model hosted by NVIDIA - advanced agentic reasoning"
            ),
            "deepseek-ai/deepseek-v4-flash": (
                "third-party model hosted by NVIDIA - fast agentic reasoning"
            ),
        },
        validation_model="nvidia/nemotron-3-nano-30b-a3b",
        setup_warning=(
            "NVIDIA hosted Free Endpoints are rate-limited development endpoints for "
            "prototyping; availability is not a production SLA and may vary by account."
        ),
        notes=(
            "Hosted NVIDIA NIM OpenAI-compatible API. The live catalog includes models "
            "from NVIDIA and third parties; reasoning controls are model-specific."
        ),
    ),
    ProfilePreset(
        key="qwen-intl",
        provider_key="qwen",
        label="Alibaba Qwen / DashScope (Intl)",
        protocol="openai_compat",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        suggested_models=(
            "qwen3.7-plus",
            "qwen3.8-max",
            "qwen3.7-max",
            "qwen3-coder-plus",
            "qwen3-coder-next",
            "qwen3.6-flash",
            "qwen-flash",
        ),
        suggested_model_descriptions={
            "qwen3.7-plus": "default - 1M context, balanced cost",
            "qwen3.8-max": "advanced - newest multimodal flagship, 1M context",
            "qwen3.7-max": "fallback - previous flagship, 1M context",
            "qwen3-coder-plus": "coding - 1M context, long-repo work",
            "qwen3-coder-next": "agentic - newest coder, 256K context",
            "qwen3.6-flash": "fast - lower-latency, 1M context",
            "qwen-flash": "economy - cheapest 1M-context option",
        },
        validation_model="qwen-flash",
        web_search_adapter=DASHSCOPE_CHAT_ADAPTER,
        web_search_model="qwen3.7-plus",
        cache_capability=_QWEN_DIAGNOSTIC_CACHE_CAPABILITY,
        setup_warning=(
            "DashScope API keys are region-specific; use a key from the Singapore region."
        ),
    ),
    ProfilePreset(
        key="qwen-us",
        provider_key="qwen",
        label="Alibaba Qwen / DashScope (US)",
        protocol="openai_compat",
        base_url="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        suggested_models=(
            "qwen3.7-plus",
            "qwen3.8-max",
            "qwen3.7-max",
            "qwen3.6-flash",
            "qwen-flash",
        ),
        suggested_model_descriptions={
            "qwen3.7-plus": "default - 1M context, balanced cost",
            "qwen3.8-max": "advanced - newest multimodal flagship, 1M context",
            "qwen3.7-max": "fallback - previous flagship, 1M context",
            "qwen3.6-flash": "fast - lower-latency, 1M context",
            "qwen-flash": "economy - cheapest 1M-context option",
        },
        validation_model="qwen-flash",
        web_search_adapter=DASHSCOPE_CHAT_ADAPTER,
        web_search_model="qwen3.7-plus",
        cache_capability=_QWEN_DIAGNOSTIC_CACHE_CAPABILITY,
        setup_warning=(
            "DashScope API keys are region-specific; use a key from the US region. "
            "Qwen coder models are not served from US (Virginia) — use qwen3.7-plus "
            "for code work."
        ),
    ),
    ProfilePreset(
        key="qwen-cn",
        provider_key="qwen",
        label="Alibaba Qwen / DashScope (China)",
        protocol="openai_compat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        suggested_models=(
            "qwen3.7-plus",
            "qwen3.8-max",
            "qwen3.7-max",
            "qwen3-coder-plus",
            "qwen3-coder-next",
            "qwen3.6-flash",
            "qwen-flash",
        ),
        suggested_model_descriptions={
            "qwen3.7-plus": "default - 1M context, balanced cost",
            "qwen3.8-max": "advanced - newest multimodal flagship, 1M context",
            "qwen3.7-max": "fallback - previous flagship, 1M context",
            "qwen3-coder-plus": "coding - 1M context, long-repo work",
            "qwen3-coder-next": "agentic - newest coder, 256K context",
            "qwen3.6-flash": "fast - lower-latency, 1M context",
            "qwen-flash": "economy - cheapest 1M-context option",
        },
        validation_model="qwen-flash",
        web_search_adapter=DASHSCOPE_CHAT_ADAPTER,
        web_search_model="qwen3.7-plus",
        cache_capability=_QWEN_DIAGNOSTIC_CACHE_CAPABILITY,
        setup_warning="DashScope API keys are region-specific; use a key from the China region.",
    ),
    ProfilePreset(
        key="zhipu",
        provider_key="zhipu",
        label="Zhipu / GLM",
        protocol="openai_compat",
        base_url="https://open.bigmodel.cn/api/paas/v4/",
        api_key_env="ZHIPUAI_API_KEY",
        suggested_models=(
            "glm-5.2",
            "glm-5.1",
            "glm-5-turbo",
            "glm-4.7",
            "glm-4.7-flashx",
            "glm-4.7-flash",
        ),
        suggested_model_descriptions={
            "glm-5.2": "default - 1M context, agentic coding",
            "glm-5.1": "advanced - previous flagship, 200K context",
            "glm-5-turbo": "coding - 200K context, cheaper than glm-5.1",
            "glm-4.7": "fallback - cheap 200K context",
            "glm-4.7-flashx": "fast - 200K context, no free-tier rate caps",
            "glm-4.7-flash": "economy - free tier, 200K context, rate limited",
        },
        # No aliases on purpose: glm-5, glm-4.6 etc. remain individually priced
        # and callable — remapping would silently change what users are billed.
        validation_model="glm-4.7-flash",
        web_search_adapter=ZHIPU_WEB_SEARCH_ADAPTER,
        web_search_model="glm-5.1",
    ),
    ProfilePreset(
        key="zai-coding-plan",
        provider_key="zai_coding_plan",
        label="Z.AI GLM Coding Plan",
        protocol="openai_compat",
        base_url="https://api.z.ai/api/coding/paas/v4",
        api_key_env="ZAI_API_KEY",
        suggested_models=(
            "glm-5.3",
            "glm-5-turbo",
            "glm-4.7",
        ),
        suggested_model_descriptions={
            "glm-5.3": "default - latest agentic coding model, 1M context",
            "glm-5-turbo": "fast - lower-credit agent model, 200K context",
            "glm-4.7": "fallback - lowest-credit plan model, 200K context",
        },
        # GLM-4.7 consumes fewer plan credits than GLM-5.3 and is available on
        # every Coding Plan tier, so use it for the initial credential probe.
        validation_model="glm-4.7",
        cache_capability=_ZAI_CODING_PLAN_CACHE_CAPABILITY,
        setup_warning=(
            "Requires a Z.AI GLM Coding Plan key; general pay-as-you-go and "
            "open.bigmodel.cn keys use different endpoints. Z.AI limits plan benefits "
            "to supported coding tools, so verify Alysis Code eligibility for your account."
        ),
        notes=(
            "Subscription Coding Plan endpoint, not the general Z.AI or China Zhipu API. "
            "All plan tiers currently offer GLM-5.3, GLM-5-Turbo, and GLM-4.7; "
            "GLM-5.2/5.1 requests are routed by the server to GLM-5.3."
        ),
    ),
    ProfilePreset(
        key="moonshot",
        provider_key="moonshot",
        label="Kimi",
        protocol="openai_compat",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        suggested_models=(
            "kimi-k2.7-code",
            "kimi-k3",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        ),
        suggested_model_descriptions={
            # k2.7-code is the deliberate default: k3 is always-thinking at
            # pinned max effort and ~3x the input price — escalate to it when a
            # task needs the 1M window, don't route routine turns through it.
            "kimi-k2.7-code": "default - 256K context, long-horizon agentic coding",
            "kimi-k3": "advanced - 1M context, always-thinking at max effort",
            "kimi-k2.7-code-highspeed": "fast - ~180 tok/s coding variant, 256K context",
            "kimi-k2.6": "fallback - 256K context, thinking toggleable",
        },
        model_aliases={
            # Only kimi-k2.6 accepts a thinking-off flag; K2.7/K3 error on it.
            # kimi-k2.5 and the moonshot-v1-* family end 2026-08-31.
            "kimi-k2": "kimi-k2.6",
            "kimi-k2.5": "kimi-k2.6",
            "kimi-k2-thinking": "kimi-k2.6",
            "kimi-k2-thinking-turbo": "kimi-k2.6",
            "kimi-k2-0905-preview": "kimi-k2.6",
            "kimi-k2-0711-preview": "kimi-k2.6",
            "kimi-k2-turbo-preview": "kimi-k2.7-code-highspeed",
            "kimi-latest": "kimi-k2.6",
            "kimi-thinking-preview": "kimi-k2.6",
            "moonshot-v1-8k": "kimi-k2.6",
            "moonshot-v1-32k": "kimi-k2.6",
            "moonshot-v1-128k": "kimi-k2.6",
            "moonshot-v1-auto": "kimi-k2.6",
        },
        validation_model="kimi-k2.6",
        web_search_adapter=MOONSHOT_KIMI_ADAPTER,
        # kimi-k3 cannot disable thinking, which Kimi's $web_search tool requires,
        # so provider-hosted search stays pinned to kimi-k2.6.
        web_search_model="kimi-k2.6",
        cache_capability=_MOONSHOT_AUTOMATIC_CACHE_CAPABILITY,
        setup_warning=(
            "Moonshot API keys are region-scoped; use a key from the international "
            "platform (platform.kimi.ai) with this endpoint."
        ),
    ),
    ProfilePreset(
        key="kimi-code",
        provider_key="moonshot",
        label="Kimi Code",
        protocol="openai_compat",
        base_url="https://api.kimi.com/coding/v1",
        api_key_env="KIMI_API_KEY",
        suggested_models=("k3", "kimi-for-coding", "kimi-for-coding-highspeed"),
        suggested_model_descriptions={
            # Tier gating: kimi-for-coding = all members; k3 = Moderato+ (256K)
            # and 1M only on Allegretto+; -highspeed = Allegretto+ only.
            # Disabling thinking on this endpoint silently routes to K2.6.
            "k3": "default - 256K context, 1M on Allegretto+",
            "kimi-for-coding": "coding - 256K context, all membership tiers",
            "kimi-for-coding-highspeed": "fast - 256K context, Allegretto tier or above",
        },
        model_aliases={
            # Cross-endpoint remaps: these are live, DIFFERENT ids on
            # platform.moonshot.ai — legal only inside this preset's alias table.
            "kimi-k3": "k3",
            "kimi-k2.7-code": "kimi-for-coding",
            "kimi-k2.7-code-highspeed": "kimi-for-coding-highspeed",
        },
        # Validation is a billed call against metered membership quota — no
        # /models endpoint exists on this surface.
        validation_model="kimi-for-coding",
        setup_warning=(
            "Requires a Kimi membership key from the kimi.com console; "
            "platform.kimi.ai pay-as-you-go keys are not valid here. "
            "Turning reasoning off routes requests to K2.6 (a different model)."
        ),
    ),
    ProfilePreset(
        key="moonshot-cn",
        provider_key="moonshot",
        label="Kimi (China)",
        protocol="openai_compat",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        suggested_models=(
            "kimi-k2.7-code",
            "kimi-k3",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        ),
        suggested_model_descriptions={
            # k2.7-code is the deliberate default: k3 is always-thinking at
            # pinned max effort and ~3x the input price — escalate to it when a
            # task needs the 1M window, don't route routine turns through it.
            "kimi-k2.7-code": "default - 256K context, long-horizon agentic coding",
            "kimi-k3": "advanced - 1M context, always-thinking at max effort",
            "kimi-k2.7-code-highspeed": "fast - ~180 tok/s coding variant, 256K context",
            "kimi-k2.6": "fallback - 256K context, thinking toggleable",
        },
        model_aliases={
            # Only kimi-k2.6 accepts a thinking-off flag; K2.7/K3 error on it.
            # kimi-k2.5 and the moonshot-v1-* family end 2026-08-31.
            "kimi-k2": "kimi-k2.6",
            "kimi-k2.5": "kimi-k2.6",
            "kimi-k2-thinking": "kimi-k2.6",
            "kimi-k2-thinking-turbo": "kimi-k2.6",
            "kimi-k2-0905-preview": "kimi-k2.6",
            "kimi-k2-0711-preview": "kimi-k2.6",
            "kimi-k2-turbo-preview": "kimi-k2.7-code-highspeed",
            "kimi-latest": "kimi-k2.6",
            "kimi-thinking-preview": "kimi-k2.6",
            "moonshot-v1-8k": "kimi-k2.6",
            "moonshot-v1-32k": "kimi-k2.6",
            "moonshot-v1-128k": "kimi-k2.6",
            "moonshot-v1-auto": "kimi-k2.6",
        },
        validation_model="kimi-k2.6",
        web_search_adapter=MOONSHOT_KIMI_ADAPTER,
        # kimi-k3 cannot disable thinking, which Kimi's $web_search tool requires,
        # so provider-hosted search stays pinned to kimi-k2.6.
        web_search_model="kimi-k2.6",
        cache_capability=_MOONSHOT_AUTOMATIC_CACHE_CAPABILITY,
        setup_warning=(
            "Moonshot API keys are region-scoped; use a key from the mainland-China "
            "platform (platform.moonshot.cn) with this endpoint."
        ),
    ),
    ProfilePreset(
        key="minimax",
        provider_key="minimax",
        label="MiniMax",
        protocol="openai_compat",
        base_url="https://api.minimax.io/v1",
        api_key_env="MINIMAX_API_KEY",
        suggested_models=(
            "MiniMax-M3",
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-M2.5",
        ),
        suggested_model_descriptions={
            # No thinking/reasoning toggle is documented for any MiniMax model —
            # send no reasoning-control parameter on this preset. M3 input above
            # 512K bills at a higher long-context rate.
            "MiniMax-M3": "default - 1M context, multimodal agentic coding",
            "MiniMax-M2.7": "coding - 200K context, prior flagship",
            "MiniMax-M2.7-highspeed": "fast - same weights as M2.7, latency-tuned",
            "MiniMax-M2.5": "fallback - stable prior generation",
        },
        model_aliases={
            "MiniMax-M2": "MiniMax-M2.7",
        },
        validation_model="MiniMax-M2.5",
        web_search_adapter=MINIMAX_CODING_PLAN_ADAPTER,
        setup_warning=(
            "MiniMax hosted web search requires a Token Plan key; pay-as-you-go model keys "
            "cannot call the Token Plan search endpoint."
        ),
        notes=(
            "Chat uses the OpenAI-compatible MiniMax API. Web search uses MiniMax's Token Plan "
            "search endpoint when the configured key has Token Plan access."
        ),
    ),
    ProfilePreset(
        key="xiaomi-mimo",
        provider_key="xiaomi",
        label="Xiaomi MiMo",
        protocol="openai_compat",
        base_url="https://api.xiaomimimo.com/v1",
        api_key_env="XIAOMI_API_KEY",
        suggested_models=("mimo-v2.5-pro", "mimo-v2-flash", "mimo-v2.5"),
        suggested_model_descriptions={
            "mimo-v2.5-pro": "default - flagship reasoning, coding & agents (1M context)",
            "mimo-v2-flash": "faster & lighter (256K context)",
            "mimo-v2.5": "omni - text + image understanding (1M context)",
        },
        validation_model="mimo-v2.5-pro",
        # Migrate the legacy bare "mimo" placeholder up to the flagship model.
        model_aliases={"mimo": "mimo-v2.5-pro"},
    ),
    ProfilePreset(
        key="bytedance",
        provider_key="bytedance",
        label="ByteDance Doubao",
        protocol="openai_compat",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_env="ARK_API_KEY",
        suggested_models=(
            "doubao-seed-2-0-pro-260215",
            "doubao-seed-2-0-code-preview-260215",
            "doubao-seed-2-0-lite-260215",
            "doubao-seed-2-0-mini-260215",
        ),
        suggested_model_descriptions={
            "doubao-seed-2-0-pro-260215": "default - flagship seed 2.0, agentic tasks",
            "doubao-seed-2-0-code-preview-260215": "coding - 256K context, preview snapshot",
            "doubao-seed-2-0-lite-260215": "fast - balanced quality and latency",
            "doubao-seed-2-0-mini-260215": "economy - cheapest seed 2.0, high concurrency",
        },
        validation_model="doubao-seed-2-0-mini-260215",
        web_search_adapter=VOLCENGINE_WEB_SEARCH_ADAPTER,
        setup_warning=(
            "Model ids rest on registry evidence only (Ark docs are not "
            "machine-readable) — verify with a live Ark key; Ark may require "
            "endpoint ids (ep-...) instead of bare model names."
        ),
    ),
    ProfilePreset(
        key="groq",
        provider_key="groq",
        label="Groq",
        protocol="openai_compat",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        suggested_models=(
            "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b",
            "openai/gpt-oss-20b",
            "groq/compound",
        ),
        suggested_model_descriptions={
            # groq/compound runs server-side built-in tools and does NOT accept
            # client tool_call — never route normal agent tool loops to it.
            "openai/gpt-oss-120b": "default - 131K context, adjustable reasoning",
            "qwen/qwen3.6-27b": "coding - thinking modes and vision, preview tier",
            "openai/gpt-oss-20b": "fast - cheapest non-deprecated production id",
            "groq/compound": "agentic - server-side web search and code exec",
        },
        model_aliases={
            # Both llama ids shut down 2026-08-16 (Groq deprecations table);
            # the other retired ids remap per the same table.
            "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
            "llama-3.1-8b-instant": "openai/gpt-oss-20b",
            "qwen/qwen3-32b": "openai/gpt-oss-120b",
            "meta-llama/llama-4-scout-17b-16e-instruct": "openai/gpt-oss-120b",
            "meta-llama/llama-4-maverick-17b-128e-instruct": "openai/gpt-oss-120b",
            "moonshotai/kimi-k2-instruct": "openai/gpt-oss-120b",
            "moonshotai/kimi-k2-instruct-0905": "openai/gpt-oss-120b",
        },
        validation_model="openai/gpt-oss-20b",
        web_search_adapter=GROQ_COMPOUND_ADAPTER,
        web_search_model="groq/compound-mini",
        setup_warning=(
            "Groq is mostly OpenAI-compatible; avoid preview-only models as production "
            "defaults (qwen/qwen3.6-27b is preview and may be pulled without notice)."
        ),
    ),
    ProfilePreset(
        key="cerebras",
        provider_key="cerebras",
        label="Cerebras",
        protocol="openai_compat",
        base_url="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
        suggested_models=(
            "gpt-oss-120b",
            "zai-glm-4.7",
            "gemma-4-31b",
        ),
        suggested_model_descriptions={
            # Context values are the free-tier floor (65K); paid keys get 131K.
            # gpt-oss-120b cannot disable reasoning (effort low|medium|high).
            "gpt-oss-120b": "default - only GA public model, ~3000 tok/s",
            "zai-glm-4.7": "coding - strongest here, deprecates 2026-08-17",
            "gemma-4-31b": "fallback - only image-input model, preview tier",
        },
        model_aliases={
            # The llama family left Cerebras public endpoints 2026-02-16 (and
            # "llama3.3-70b" was never a valid spelling of the id).
            "llama3.3-70b": "gpt-oss-120b",
            "llama-3.3-70b": "gpt-oss-120b",
            "llama3.1-70b": "gpt-oss-120b",
            "llama3.1-8b": "gpt-oss-120b",
            "qwen-3-32b": "gpt-oss-120b",
            "qwen-3-coder-480b": "zai-glm-4.7",
            "zai-glm-4.6": "zai-glm-4.7",
            "deepseek-r1-distill-llama-70b": "gpt-oss-120b",
        },
        validation_model="gpt-oss-120b",
    ),
    ProfilePreset(
        key="mistral",
        provider_key="mistral",
        label="Mistral AI",
        protocol="openai_compat",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        suggested_models=(
            "mistral-medium-3-5",
            "mistral-large-2512",
            "mistral-small-2603",
            "codestral-2508",
            "ministral-8b-2512",
        ),
        suggested_model_descriptions={
            # codestral is FIM/completion-oriented with ~4K max output — routers
            # should prefer the default for multi-file agentic patch turns.
            "mistral-medium-3-5": "default - agentic and coding flagship, 256K",
            "mistral-large-2512": "advanced - mistral large 3, 675B MoE, 256K",
            "mistral-small-2603": "fast - mistral small 4, low latency",
            "codestral-2508": "coding - FIM and completion, 4K max output",
            "ministral-8b-2512": "economy - small tool-capable model",
        },
        model_aliases={
            # Mistral documents mistral-medium-3-5 as the primary API id. Keep
            # the former Alysis Code default as a compatibility alias, while the
            # provider-managed -latest alias passes through unchanged.
            "mistral-medium-2604": "mistral-medium-3-5",
            "mistral-medium-3": "mistral-medium-3-5",
            "mistral-medium-2508": "mistral-medium-3-5",
            "mistral-medium-2505": "mistral-medium-3-5",
            "mistral-small-latest": "mistral-small-2603",
            "mistral-small-2506": "mistral-small-2603",
            "mistral-large-latest": "mistral-large-2512",
            "mistral-large-2411": "mistral-medium-3-5",
            "mistral-large-2407": "mistral-large-2512",
            "codestral-latest": "codestral-2508",
            "devstral-2512": "mistral-medium-3-5",
            "devstral-latest": "mistral-medium-3-5",
            "devstral-medium-latest": "mistral-medium-3-5",
            "devstral-medium-2507": "mistral-medium-3-5",
            "devstral-small-2507": "mistral-small-2603",
            "labs-devstral-small-2512": "mistral-medium-3-5",
            "magistral-medium-latest": "mistral-medium-3-5",
            "magistral-small-latest": "mistral-small-2603",
            "ministral-8b-latest": "ministral-8b-2512",
            "open-mistral-nemo-2407": "ministral-8b-2512",
        },
        validation_model="ministral-3b-2512",
        web_search_adapter=MISTRAL_CONVERSATIONS_ADAPTER,
        web_search_model="mistral-medium-latest",
        cache_capability=_MISTRAL_PROMPT_CACHE_CAPABILITY,
    ),
    ProfilePreset(
        key="xai",
        provider_key="xai",
        label="xAI Grok",
        protocol="openai_compat",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        suggested_models=(
            "grok-4.6",
            "grok-4.5",
            "grok-build-0.1",
            "grok-4.3",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
        ),
        suggested_model_descriptions={
            # grok-build-0.1 is served from us-east-1/us-west-2 only. Max output
            # is unpublished for the 4.20 family — clamp conservatively.
            "grok-4.6": "default - newest flagship for coding and agents, 500K",
            "grok-4.5": "fallback - previous flagship for coding and agents",
            "grok-build-0.1": "coding - agentic engineering model, 256K",
            "grok-4.3": "advanced - 1M context window",
            "grok-4.20-0309-reasoning": "reasoning - dedicated snapshot, 1M context",
            "grok-4.20-0309-non-reasoning": "fast - no-reasoning snapshot, 1M context",
        },
        model_aliases={
            # Retired 2026-05-15, full shutdown 2026-08-15. The *-non-reasoning
            # slugs deliberately map to the non-reasoning snapshot (xAI's own
            # redirect lands them on grok-4.3 with effort=none, which the alias
            # table cannot express).
            "grok-code-fast-1": "grok-build-0.1",
            "grok-4": "grok-4.3",
            "grok-4-0709": "grok-4.3",
            "grok-4-fast": "grok-4.3",
            "grok-4.3-latest": "grok-4.3",
            "grok-4-fast-reasoning": "grok-4.3",
            "grok-4-1-fast-reasoning": "grok-4.3",
            "grok-4-fast-non-reasoning": "grok-4.20-0309-non-reasoning",
            "grok-4-1-fast-non-reasoning": "grok-4.20-0309-non-reasoning",
            "grok-3": "grok-4.3",
        },
        validation_model="grok-4.20-0309-non-reasoning",
        web_search_adapter=XAI_RESPONSES_ADAPTER,
        cache_capability=_XAI_CONVERSATION_HEADER_CACHE_CAPABILITY,
        setup_warning=(
            "Retired slugs (grok-4, grok-4-fast, grok-3, grok-code-fast-1) shut down "
            "fully 2026-08-15 and are billed at grok-4.3 rates until then; migrate "
            "pinned configs explicitly. Ids use dots, not dashes (grok-4.6)."
        ),
    ),
    ProfilePreset(
        key="cohere",
        provider_key="cohere",
        label="Cohere (compat)",
        protocol="openai_compat",
        base_url="https://api.cohere.ai/compatibility/v1",
        api_key_env="COHERE_API_KEY",
        suggested_models=(
            "command-a-plus-05-2026",
            "command-a-reasoning-08-2025",
            "command-a-03-2025",
            "command-r7b-12-2024",
        ),
        suggested_model_descriptions={
            # Reasoning toggle (thinking=disabled) is a native Chat V2 param and
            # may not pass through /compatibility/v1 — treat as thinking-on.
            "command-a-plus-05-2026": "default - newest command a+, 128K context",
            "command-a-reasoning-08-2025": "reasoning - 256K context, thinking is a toggle",
            "command-a-03-2025": "advanced - 256K context, prior flagship",
            "command-r7b-12-2024": "economy - cheapest live chat model, 128K",
        },
        model_aliases={
            "command": "command-a-03-2025",
            "command-light": "command-r-08-2024",
            "command-r": "command-r-08-2024",
            "command-r-plus": "command-r-plus-08-2024",
        },
        validation_model="command-r7b-12-2024",
        web_search_adapter=COHERE_WEB_SEARCH_ADAPTER,
        setup_warning=(
            "Cohere shut down the v1 hosted web-search connector on 2025-09-15; "
            "hosted web search on this preset needs migration to an external "
            "search adapter."
        ),
        notes=(
            "Chat uses Cohere's OpenAI compatibility API (api.cohere.ai/compatibility/v1 — "
            "documented and correct; do not migrate to v2/chat). The v1 hosted web-search "
            "connector this preset's adapter targeted was shut down 2025-09-15."
        ),
    ),
    ProfilePreset(
        key="openrouter",
        provider_key="openrouter",
        label="OpenRouter (gateway)",
        protocol="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        suggested_models=(
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.8",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-luna",
            "z-ai/glm-5.2",
            "deepseek/deepseek-v4-pro-0813",
            "deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-v4-flash-vision-exp",
            "qwen/qwen3.8-max",
        ),
        suggested_model_descriptions={
            # Vendor prefixes are exact: z-ai/ (not zai/), x-ai/, moonshotai/.
            # Avoid '-latest' floating aliases and rate-limited :free variants
            # for agent loops.
            "anthropic/claude-sonnet-5": "default - coding and agents, 1M context",
            "anthropic/claude-opus-4.8": "advanced - long-horizon autonomous work",
            "openai/gpt-5.6-terra": "coding - balanced gpt-5.6 tier, 1.05M context",
            "openai/gpt-5.6-luna": "fast - cost-efficient gpt-5.6 tier",
            "z-ai/glm-5.2": "economy - cheap 1M-context tool caller",
            "deepseek/deepseek-v4-pro-0813": "agentic - current reasoning MoE, 1M context",
            "deepseek/deepseek-v4-flash-0731": "fast - current low-cost 1M release",
            "deepseek/deepseek-v4-flash-vision-exp": (
                "vision preview - image understanding and tools, 1M context"
            ),
            "qwen/qwen3.8-max": "multimodal - flagship Qwen agent model, 1M context",
        },
        validation_model="deepseek/deepseek-v4-flash-0731",
        web_search_adapter=OPENROUTER_WEB_ADAPTER,
        cache_capability=_OPENROUTER_STICKY_SESSION_CACHE_CAPABILITY,
        setup_warning=(
            "OpenRouter routes through upstream providers; availability, pricing, privacy, "
            "and parameter support can vary by route."
        ),
        notes="Single API to many providers' models.",
    ),
    ProfilePreset(
        key="perplexity",
        provider_key="perplexity",
        label="Perplexity Sonar",
        protocol="openai_compat",
        base_url="https://api.perplexity.ai",
        api_key_env="PERPLEXITY_API_KEY",
        suggested_models=("sonar-pro", "sonar"),
        web_search_adapter=PERPLEXITY_SONAR_ADAPTER,
        web_search_model="sonar",
        setup_warning=(
            "Search-only: sonar models reject tool definitions (HTTP 400), so this "
            "preset cannot run agentic tool loops. Perplexity's coding models live "
            "on the Agent API (/v1/agent), which needs a Responses-style client "
            "Alysis Code does not ship yet."
        ),
        notes="Sonar models include web-grounded answers and citations.",
    ),
    ProfilePreset(
        key="together",
        provider_key="together",
        label="Together AI",
        protocol="openai_compat",
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
        suggested_models=(
            "zai-org/GLM-5.2",
            "moonshotai/Kimi-K2.7-Code",
            "deepseek-ai/DeepSeek-V4-Pro-0813",
            "deepseek-ai/DeepSeek-V4-Flash-0731",
            "MiniMaxAI/MiniMax-M3",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
        ),
        suggested_model_descriptions={
            # Ids are case-sensitive and vendor-prefixed. Kimi-K2.7-Code and
            # MiniMax-M3 reason unconditionally — never emit a reasoning-off or
            # effort param for them.
            "zai-org/GLM-5.2": "default - general coding, 256K context",
            "moonshotai/Kimi-K2.7-Code": "coding - code specialist, 256K context",
            "deepseek-ai/DeepSeek-V4-Pro-0813": "reasoning - current flagship, 1M context",
            "deepseek-ai/DeepSeek-V4-Flash-0731": "fast - current 1M-context release",
            "MiniMaxAI/MiniMax-M3": "economy - cheapest 512K-context option",
            "openai/gpt-oss-120b": "open - larger tool-capable GPT-OSS model, 128K context",
            "openai/gpt-oss-20b": "fallback - cheapest tool-capable id",
        },
        model_aliases={
            # Fallback policy, NOT vendor renames: Together retires serverless
            # models with a blank successor column. Two are cross-vendor
            # substitutions — surface the swap to the user at resolution time.
            "zai-org/GLM-5.1": "zai-org/GLM-5.2",
            "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8": "moonshotai/Kimi-K2.7-Code",
            "Qwen/Qwen3-Coder-Next-FP8": "moonshotai/Kimi-K2.7-Code",
        },
        validation_model="openai/gpt-oss-20b",
        setup_warning=(
            "Together retires serverless models on a published schedule with no "
            "successor mapping — expect id churn; verify access with Together's "
            "Models API."
        ),
    ),
    ProfilePreset(
        key="fireworks",
        provider_key="fireworks",
        label="Fireworks AI",
        protocol="openai_compat",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
        suggested_models=(
            "accounts/fireworks/models/glm-5p2",
            "accounts/fireworks/models/kimi-k2p7-code",
            "accounts/fireworks/models/deepseek-v4-pro-0813",
            "accounts/fireworks/models/deepseek-v4-flash-0731",
            "accounts/fireworks/models/minimax-m3",
            "accounts/fireworks/models/qwen3p7-plus",
        ),
        suggested_model_descriptions={
            # 'p' is the decimal convention (5p2 = 5.2). Catalog membership does
            # NOT imply serverless availability on Fireworks — every id here is
            # confirmed serverless-capable.
            "accounts/fireworks/models/glm-5p2": "default - general agentic coding, 1M context",
            "accounts/fireworks/models/kimi-k2p7-code": "coding - 262K context, tool calling",
            "accounts/fireworks/models/deepseek-v4-pro-0813": (
                "reasoning - current 1M-context production release"
            ),
            "accounts/fireworks/models/deepseek-v4-flash-0731": (
                "fast - current lowest-cost 1M-context release"
            ),
            "accounts/fireworks/models/minimax-m3": "economy - 512K context, effort control",
            "accounts/fireworks/models/qwen3p7-plus": "fallback - 262K context, standard tier only",
        },
        model_aliases={
            # qwen2p5-coder is not serverless-capable at all (on-demand GPU
            # only); the other two are superseded snapshots.
            "accounts/fireworks/models/qwen2p5-coder-32b-instruct": (
                "accounts/fireworks/models/kimi-k2p7-code"
            ),
            "accounts/fireworks/models/kimi-k2p6": "accounts/fireworks/models/kimi-k2p7-code",
            "accounts/fireworks/models/glm-5p1": "accounts/fireworks/models/glm-5p2",
        },
        validation_model="accounts/fireworks/models/deepseek-v4-flash-0731",
    ),
    # Account-gated hosted preset — registered after the hosted third-party
    # vendors so listings that read PROFILE_PRESETS order do not headline it.
    ProfilePreset(
        key="alysis",
        provider_key="alysis",
        label="Alysis Code Pro",
        protocol="openai_compat",
        # The Alysis Code hosted proxy (`llm` Supabase Edge Function). It
        # authenticates the user's slk_ key, meters the free daily allowance /
        # Pro credits server-side, and forwards to DeepSeek. The login flow
        # overrides this from alysis_cloud at runtime (env-configurable), so
        # this literal is just the default.
        base_url="https://vzigujbcjjmpntxhmyvr.supabase.co/functions/v1/llm/v1",
        api_key_env=None,
        # The models the subscription offers. Live availability is discovered
        # from the gateway's /v1/models at runtime; this static list is the
        # offline fallback and the menu shown before a model is chosen.
        suggested_models=("deepseek-v4-flash", "deepseek-v4-pro"),
        suggested_model_descriptions={
            "deepseek-v4-flash": "default - fast high-volume coding (1M context, free daily allowance)",
            "deepseek-v4-pro": "flagship - deeper reasoning (1M context, requires Alysis Code Pro)",
        },
        validation_model="deepseek-v4-flash",
        # Migrate ids from the retired Xiaomi MiMo trial to the Pro default so
        # old sessions keep working after upgrade.
        model_aliases={
            "mimo": "deepseek-v4-flash",
            "mimo-v2.5-pro": "deepseek-v4-flash",
            "mimo-v2-flash": "deepseek-v4-flash",
            "mimo-v2.5": "deepseek-v4-flash",
        },
        setup_warning=("Requires an Alysis Code Pro subscription — run `alysis login` to connect."),
        notes="Hosted models via your Alysis Code Pro subscription. Authenticate with `alysis login`.",
    ),
    ProfilePreset(
        key="ollama",
        provider_key="ollama",
        label="Ollama (local)",
        protocol="openai_compat",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        suggested_models=("llama3.3",),
        notes="Local Ollama server. No API key required.",
    ),
    ProfilePreset(
        key="lm-studio",
        provider_key="lm-studio",
        label="LM Studio (local)",
        protocol="openai_compat",
        base_url="http://localhost:1234/v1",
        api_key_env=None,
        suggested_models=("local-model",),
        notes="Local LM Studio server. No API key required.",
    ),
    ProfilePreset(
        key="vllm",
        provider_key="vllm",
        label="vLLM (self-hosted)",
        protocol="openai_compat",
        base_url="http://localhost:8000/v1",
        api_key_env=None,
        suggested_models=("local-model",),
    ),
    ProfilePreset(
        key="custom",
        label="Custom (specify URL manually)",
        protocol="openai_compat",
        base_url="",
        api_key_env=None,
        suggested_models=(),
        notes="Use for unlisted endpoints. Type the URL during setup.",
    ),
)

PRESET_BY_KEY: dict[str, ProfilePreset] = {preset.key: preset for preset in PROFILE_PRESETS}


def get_preset(key: str) -> ProfilePreset | None:
    return PRESET_BY_KEY.get(str(key or "").strip().lower())


def model_options_for_preset(preset: ProfilePreset) -> tuple[tuple[str, str, str], ...]:
    """Return picker rows for the models this preset intentionally supports."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for model in preset.suggested_models:
        model_id = str(model or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        description = str(preset.suggested_model_descriptions.get(model_id) or "").strip()
        rows.append((model_id, model_id, description or "suggested by provider preset"))
    return tuple(rows)


def canonical_model_alias_for_preset(preset: ProfilePreset, model: str) -> str:
    """Map explicit stale provider aliases to the preset's current model ID."""
    raw = str(model or "").strip()
    if not raw:
        return raw
    for alias, canonical in preset.model_aliases.items():
        if str(alias or "").strip().casefold() == raw.casefold():
            normalized = str(canonical or "").strip()
            return normalized or raw
    return raw


def find_preset_for_profile(profile: ProfileSpec) -> ProfilePreset | None:
    """Best-effort mapping from a persisted profile back to a known provider preset."""
    name_match = get_preset(profile.name)
    if name_match is not None and _profile_matches_preset(profile, name_match):
        return name_match

    for preset in PROFILE_PRESETS:
        if preset.key == "custom":
            continue
        if _profile_matches_preset(profile, preset):
            return preset

    if profile.protocol != OPENAI_COMPAT_PROTOCOL:
        return None
    return find_preset_for_base_url(profile.base_url)


def find_preset_for_base_url(base_url: str) -> ProfilePreset | None:
    normalized = _normalized_base_url(base_url)
    if not normalized:
        return None
    matches: list[ProfilePreset] = []
    for preset in PROFILE_PRESETS:
        if preset.key == "custom":
            continue
        if _normalized_base_url(preset.base_url) == normalized:
            matches.append(preset)
    if not matches:
        return None
    compatibility = next(
        (preset for preset in matches if preset.protocol == OPENAI_COMPAT_PROTOCOL),
        None,
    )
    return compatibility or matches[0]


def _profile_matches_preset(profile: ProfileSpec, preset: ProfilePreset) -> bool:
    if str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip() != preset.protocol:
        return False
    profile_url = _normalized_base_url(profile.base_url)
    preset_url = _normalized_base_url(preset.base_url)
    if not profile_url or not preset_url or profile_url != preset_url:
        return False
    return True


def profile_provider_family(profile: ProfileSpec) -> str | None:
    """Resolve a profile to a first-party family for protocol conversion and diagnostics."""
    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    if protocol == OPENAI_RESPONSES_PROTOCOL:
        return "openai"
    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        return "anthropic"
    if protocol == GEMINI_GENERATE_CONTENT_PROTOCOL:
        return "gemini"
    if protocol == GEMINI_INTERACTIONS_PROTOCOL:
        return "gemini"

    preset = get_preset(profile.name)
    if preset is not None:
        if preset.key in {"openai", "openai-responses"}:
            return "openai"
        if preset.key in {"anthropic", "anthropic-compat", "anthropic-native"}:
            return "anthropic"
        if preset.key in {"gemini", "gemini-compat", "gemini-native"}:
            return "gemini"

    normalized_name = str(profile.name or "").strip().lower()
    if "openai" in normalized_name:
        return "openai"
    if "anthropic" in normalized_name or "claude" in normalized_name:
        return "anthropic"
    if "gemini" in normalized_name or "google" in normalized_name:
        return "gemini"

    parsed = _split_base_url(profile.base_url)
    if parsed[0] == "api.openai.com":
        return "openai"
    if parsed[0] == "api.anthropic.com":
        return "anthropic"
    if parsed[0] == "generativelanguage.googleapis.com":
        return "gemini"
    return None


def target_preset_for_profile_conversion(
    profile: ProfileSpec,
    *,
    target: str,
) -> ProfilePreset | None:
    normalized_target = normalize_conversion_target(target)
    family = profile_provider_family(profile)
    if family is None:
        return None
    preset_key = _CONVERSION_PRESET_BY_FAMILY.get(family, {}).get(normalized_target)
    if preset_key is None:
        return None
    return get_preset(preset_key)


def normalize_conversion_target(value: str) -> str:
    target = str(value or "").strip().lower().replace("_", "-")
    if target in {"native", "first-party", "firstparty"}:
        return "native"
    if target in {"compat", "compatibility", "openai-compatible", "gateway"}:
        return "compatibility"
    raise ValueError("conversion target must be 'native' or 'compatibility'")


def convert_profile_to_preset(profile: ProfileSpec, preset: ProfilePreset) -> ProfileSpec:
    current_model = str(profile.default_model or "").strip()
    default_model = canonical_model_alias_for_preset(preset, current_model)
    target_family = _preset_provider_family(preset)
    if not default_model or _model_known_incompatible_with_family(default_model, target_family):
        default_model = preset.suggested_models[0] if preset.suggested_models else ""
    notes = _converted_profile_notes(profile, preset)

    return ProfileSpec(
        name=profile.name,
        protocol=preset.protocol,
        base_url=preset.base_url,
        api_key_env=profile.api_key_env or preset.api_key_env,
        extra_headers=dict(profile.extra_headers),
        default_model=default_model,
        reasoning_effort=profile.reasoning_effort,
        web_search_adapter=preset.web_search_adapter,
        web_search_model=preset.web_search_model,
        notes=notes,
    )


def _converted_profile_notes(profile: ProfileSpec, preset: ProfilePreset) -> str:
    notes = str(profile.notes or "").strip()
    if not notes:
        return preset.notes
    source_preset = find_preset_for_profile(profile)
    if source_preset is not None and notes == str(source_preset.notes or "").strip():
        return preset.notes

    lowered = notes.lower()
    target_is_native = preset.protocol in NATIVE_PROFILE_PROTOCOLS
    if target_is_native and (
        "openai-compat" in lowered
        or "openai compatible" in lowered
        or "openai-compatible" in lowered
        or "compatibility mode" in lowered
    ):
        return preset.notes
    if not target_is_native and "native" in lowered:
        return preset.notes
    return notes


def _split_base_url(value: str | None) -> tuple[str, str]:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return "", ""
    path = parsed.path.rstrip("/").lower()
    return (parsed.hostname or "").rstrip(".").lower(), path


def _preset_provider_family(preset: ProfilePreset) -> str | None:
    for family, targets in _CONVERSION_PRESET_BY_FAMILY.items():
        if preset.key in targets.values():
            return family
    return None


def _model_known_incompatible_with_family(model: str, family: str | None) -> bool:
    if family is None:
        return False
    normalized = model.strip().lower()
    model_family = _known_model_family(normalized)
    if model_family is None:
        return False
    if model_family != family:
        return True
    return _has_known_provider_namespace(normalized)


def known_model_family(model: str) -> str | None:
    """Best-effort model-family classifier for static diagnostics.

    This is intentionally conservative. Unknown custom gateway models return None so doctor
    diagnostics do not over-warn on valid provider-specific names Alysis Code cannot know offline.
    """
    return _known_model_family(str(model or "").strip().lower())


def model_known_incompatible_with_family(model: str, family: str | None) -> bool:
    """Public wrapper used by diagnostics and tests."""
    return _model_known_incompatible_with_family(model, family)


def _known_model_family(model: str) -> str | None:
    known_prefixes: dict[str, tuple[str, ...]] = {
        "openai": ("gpt-", "chatgpt-", "o1", "o3", "o4", "o5"),
        "anthropic": ("claude-",),
        "gemini": ("gemini-",),
    }
    known_namespaces: dict[str, tuple[str, ...]] = {
        "openai": ("openai",),
        "anthropic": ("anthropic", "anthropic-ai"),
        "gemini": ("google", "gemini"),
    }
    parts = [part for part in model.split("/") if part]
    for known_family, namespaces in known_namespaces.items():
        if parts and parts[0] in namespaces:
            return known_family
    model_id = parts[-1] if parts else model
    for known_family, prefixes in known_prefixes.items():
        if model_id.startswith(prefixes):
            return known_family
    return None


def _has_known_provider_namespace(model: str) -> bool:
    if "/" not in model:
        return False
    namespace = model.split("/", 1)[0]
    return namespace in {"openai", "anthropic", "anthropic-ai", "google", "gemini"}


def _normalized_base_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def make_profile_from_preset(
    preset: ProfilePreset,
    *,
    name: str | None = None,
) -> ProfileSpec:
    profile_name = str(name or preset.key).strip().lower()
    return ProfileSpec(
        name=profile_name,
        protocol=preset.protocol,
        base_url=preset.base_url,
        api_key_env=preset.api_key_env,
        extra_headers=dict(preset.extra_headers),
        default_model=preset.suggested_models[0] if preset.suggested_models else "",
        web_search_adapter=preset.web_search_adapter,
        web_search_model=preset.web_search_model,
        notes=preset.notes,
    )
