from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..branding import env_get
from ..config import (
    AppConfig,
    ConfigError,
    is_first_party_openai_base_url,
    resolve_web_search_adapter,
    resolve_web_search_api_key,
    resolve_web_search_base_url,
    resolve_web_search_explicit_base_url,
    resolve_web_search_mode,
    resolve_web_search_model,
    resolve_web_search_timeout_s,
)
from ..llm.metadata import endpoint_descriptor
from ..llm.openai_responses import OpenAIResponsesClient, ResponsesError
from ..llm.protocols import OPENAI_COMPAT_PROTOCOL, OPENAI_RESPONSES_PROTOCOL
from ..llm.provider_limits import ProviderRetrySettings, resolve_provider_retry_settings
from ..profiles import active_subscription_selection_ready, get_active_profile
from ..provider_telemetry import record_web_search_call
from ..safety.safe_http import Resolver
from ..web_search_adapters import (
    ANTHROPIC_MESSAGES_ADAPTER,
    AUTO_WEB_SEARCH_ADAPTER,
    COHERE_WEB_SEARCH_ADAPTER,
    DASHSCOPE_CHAT_ADAPTER,
    DDGS_ADAPTER,
    EXTERNAL_WEB_SEARCH_ADAPTERS,
    GEMINI_GROUNDING_ADAPTER,
    GROQ_COMPOUND_ADAPTER,
    MINIMAX_CODING_PLAN_ADAPTER,
    MISTRAL_CONVERSATIONS_ADAPTER,
    MOONSHOT_KIMI_ADAPTER,
    NATIVE_WEB_SEARCH_ADAPTERS,
    OPENAI_RESPONSES_ADAPTER,
    OPENROUTER_WEB_ADAPTER,
    PERPLEXITY_SONAR_ADAPTER,
    TAVILY_ADAPTER,
    VALID_WEB_SEARCH_ADAPTERS,
    VOLCENGINE_WEB_SEARCH_ADAPTER,
    WEB_SEARCH_ADAPTER_CHOICES,
    XAI_RESPONSES_ADAPTER,
    ZHIPU_WEB_SEARCH_ADAPTER,
    normalize_web_search_adapter,
    web_search_adapter_is_external,
    web_search_adapter_is_native,
)
from .web_search_dashscope import (
    DashScopeChatSearchError,
    dashscope_chat_search,
    dashscope_model_supports_web_search,
)
from .web_search_ddgs import (
    KEYLESS_WEB_SEARCH_ENV,
    DdgsSearchError,
    ddgs_package_available,
    ddgs_search,
    keyless_web_search_enabled,
)
from .web_search_provider_adapters import (
    ProviderWebSearchError,
    anthropic_messages_search,
    cohere_web_search,
    gemini_grounding_search,
    groq_compound_search,
    minimax_coding_plan_search,
    mistral_conversations_search,
    moonshot_kimi_search,
    openrouter_web_search,
    perplexity_sonar_search,
    volcengine_web_search,
    zhipu_web_search,
)
from .web_search_tavily import TavilySearchError, tavily_search


class WebSearchError(RuntimeError):
    """Raised for any web_search failure.

    ``recoverable=True`` marks failures the calling model can fix by adjusting its
    own tool arguments (input validation, argument-contract violations). The agent
    loop returns those to the model as plain errors instead of disabling web tools
    for the rest of the turn.
    """

    def __init__(self, message: str, *, recoverable: bool = False) -> None:
        super().__init__(message)
        self.recoverable = recoverable


_OPENAI_RESPONSES_PROVIDER = OPENAI_RESPONSES_ADAPTER
_XAI_RESPONSES_PROVIDER = XAI_RESPONSES_ADAPTER
_ANTHROPIC_MESSAGES_PROVIDER = ANTHROPIC_MESSAGES_ADAPTER
_GEMINI_GROUNDING_PROVIDER = GEMINI_GROUNDING_ADAPTER
_OPENROUTER_WEB_PROVIDER = OPENROUTER_WEB_ADAPTER
_DASHSCOPE_CHAT_PROVIDER = DASHSCOPE_CHAT_ADAPTER
_MOONSHOT_KIMI_PROVIDER = MOONSHOT_KIMI_ADAPTER
_ZHIPU_WEB_SEARCH_PROVIDER = ZHIPU_WEB_SEARCH_ADAPTER
_VOLCENGINE_WEB_SEARCH_PROVIDER = VOLCENGINE_WEB_SEARCH_ADAPTER
_PERPLEXITY_SONAR_PROVIDER = PERPLEXITY_SONAR_ADAPTER
_GROQ_COMPOUND_PROVIDER = GROQ_COMPOUND_ADAPTER
_MISTRAL_CONVERSATIONS_PROVIDER = MISTRAL_CONVERSATIONS_ADAPTER
_MINIMAX_CODING_PLAN_PROVIDER = MINIMAX_CODING_PLAN_ADAPTER
_COHERE_WEB_SEARCH_PROVIDER = COHERE_WEB_SEARCH_ADAPTER
_TAVILY_PROVIDER = TAVILY_ADAPTER
_DDGS_PROVIDER = DDGS_ADAPTER
_WEB_SEARCH_PROVIDER_ENV = "ALYSIS_WEB_SEARCH_PROVIDER"
_WEB_SEARCH_ADAPTER_ENV = "ALYSIS_WEB_SEARCH_ADAPTER"
_VALID_WEB_SEARCH_PROVIDERS = set(VALID_WEB_SEARCH_ADAPTERS) - {AUTO_WEB_SEARCH_ADAPTER}
_WEB_SEARCH_MODE_OFF = "off"
_WEB_SEARCH_MODE_AUTO = "auto"
_WEB_SEARCH_MODE_NATIVE = "native"
_WEB_SEARCH_MODE_EXTERNAL = "external"

# web_search runs as a one-shot tool call that the agent loop re-issues on its own
# when needed, so a slow/failed provider call should fail fast rather than burn the
# full provider retry budget. A ~20s read timeout retried 5x is ~148s of dead air
# before the agent's own retry even gets a chance. Cap web_search provider retries
# low; a transient blip still gets one quick retry.
_WEB_SEARCH_MAX_PROVIDER_RETRIES = 1

# OpenRouter web_search runs a full upstream chat round-trip *plus* the web search,
# and the hosted Alysis Code MiMo trial proxy adds a slow reasoning model on top, so a
# short web_search_timeout_s starves it (a 20s budget times out mid-read). Floor the
# OpenRouter web adapter's per-attempt budget so an under-configured timeout still has
# time to return results; an explicitly higher web_search_timeout_s is preserved.
_OPENROUTER_WEB_MIN_TIMEOUT_S = 60.0


# After a native backend hard-fails in ``auto`` mode and an external backend
# serves the result, remember that per session: re-attempting the broken native
# backend on every call wastes a full failed round-trip before each fallback.
# The preference is dropped as soon as the remembered backend stops working or
# the native runtime it shadowed is reconfigured (fingerprint mismatch), so a
# recovered or fixed native backend is retried on the next call after that.
_AUTO_FALLBACK_PREFERENCES_LOCK = RLock()
_AUTO_FALLBACK_PREFERENCE_BY_SESSION: dict[str, tuple[str, str]] = {}


def _web_search_runtime_fingerprint(runtime: WebSearchRuntimeConfig) -> str:
    key_digest = sha256(str(runtime.api_key or "").encode("utf-8")).hexdigest()[:16]
    return "\n".join(
        [
            str(runtime.provider or ""),
            str(runtime.base_url or ""),
            str(runtime.model or ""),
            key_digest,
        ]
    )


def _record_session_fallback_preference(
    session_id: str | None,
    provider: str,
    *,
    failed_runtime_fingerprint: str,
) -> None:
    if not session_id:
        return
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        _AUTO_FALLBACK_PREFERENCE_BY_SESSION[session_id] = (
            provider,
            failed_runtime_fingerprint,
        )


def _session_fallback_preference(
    session_id: str | None,
    *,
    current_runtime_fingerprint: str,
) -> str | None:
    """Return the remembered fallback backend, or None when it no longer applies.

    A preference recorded against a differently-configured native runtime is
    discarded: the user changing the web-search configuration mid-session must
    give the newly configured backend a fresh chance.
    """

    if not session_id:
        return None
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        stored = _AUTO_FALLBACK_PREFERENCE_BY_SESSION.get(session_id)
        if stored is None:
            return None
        provider, failed_fingerprint = stored
        if failed_fingerprint != current_runtime_fingerprint:
            _AUTO_FALLBACK_PREFERENCE_BY_SESSION.pop(session_id, None)
            return None
        return provider


def _clear_session_fallback_preference(session_id: str | None) -> None:
    if not session_id:
        return
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        _AUTO_FALLBACK_PREFERENCE_BY_SESSION.pop(session_id, None)


def _reset_web_search_session_state_for_tests() -> None:
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        _AUTO_FALLBACK_PREFERENCE_BY_SESSION.clear()


def _web_search_provider_retry_settings(cfg: AppConfig | None) -> ProviderRetrySettings:
    base = resolve_provider_retry_settings(cfg)
    capped = min(base.max_retries, _WEB_SEARCH_MAX_PROVIDER_RETRIES)
    if capped == base.max_retries:
        return base
    return ProviderRetrySettings(
        max_retries=capped,
        base_delay_seconds=base.base_delay_seconds,
        max_delay_seconds=base.max_delay_seconds,
    )


@dataclass(frozen=True)
class WebSearchRuntimeConfig:
    provider: str
    base_url: str | None
    api_key: str
    model: str | None
    timeout_s: float
    auth_provider: str | None = None


@dataclass(frozen=True)
class WebSearchRuntimeStatus:
    mode: str
    provider: str | None
    base_url: str | None
    model: str | None
    api_key_available: bool
    registration_ready: bool
    notes: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "provider": self.provider,
            "base_url_descriptor": (endpoint_descriptor(self.base_url) if self.base_url else None),
            "model": self.model,
            "api_key_available": self.api_key_available,
            "registration_ready": self.registration_ready,
            "setup_hint": self.setup_hint,
            "notes": list(self.notes),
        }

    @property
    def availability_label(self) -> str:
        if self.registration_ready:
            return "available"
        if self.mode == _WEB_SEARCH_MODE_OFF:
            return "disabled"
        return f"{self.mode}-unavailable"

    @property
    def summary(self) -> str:
        if self.registration_ready and self.provider:
            return f"available via {self.provider}"
        if len(self.notes) > 1 and self.notes[0].startswith("explicit adapter selected "):
            return self.notes[1]
        if self.notes:
            return self.notes[0]
        return "unavailable"

    @property
    def setup_hint(self) -> str:
        if self.registration_ready:
            if self.provider == _TAVILY_PROVIDER:
                return (
                    "Provider-agnostic web search is ready via the external search key; chat and "
                    "top-level Plan/readonly sessions can use it."
                )
            if self.provider == _DDGS_PROVIDER:
                return (
                    "Keyless web search (DuckDuckGo metasearch) is ready — no API key needed; "
                    "set TAVILY_API_KEY for a higher-quality external backend."
                )
            if self.provider == _DASHSCOPE_CHAT_PROVIDER:
                return (
                    "Native DashScope/Qwen web search is ready; chat and top-level "
                    "Plan/readonly sessions can use it."
                )
            if self.provider == _MOONSHOT_KIMI_PROVIDER:
                return "Native Moonshot/Kimi web_search is ready."
            if self.provider == _ZHIPU_WEB_SEARCH_PROVIDER:
                return "Native Zhipu/GLM web_search is ready."
            if self.provider == _VOLCENGINE_WEB_SEARCH_PROVIDER:
                return "Native Volcengine/Doubao web_search is ready."
            if self.provider == _OPENAI_RESPONSES_PROVIDER:
                return (
                    "Native OpenAI Responses web search is ready; chat and top-level "
                    "Plan/readonly sessions can use it."
                )
            if self.provider == _XAI_RESPONSES_PROVIDER:
                return "Native xAI Responses web_search is ready."
            if self.provider == _ANTHROPIC_MESSAGES_PROVIDER:
                return "Native Anthropic Messages web_search is ready."
            if self.provider == _GEMINI_GROUNDING_PROVIDER:
                return "Native Gemini Google Search grounding is ready."
            if self.provider == _OPENROUTER_WEB_PROVIDER:
                return "OpenRouter server-side web_search is ready."
            if self.provider == _PERPLEXITY_SONAR_PROVIDER:
                return "Perplexity Sonar web-grounded search is ready."
            if self.provider == _GROQ_COMPOUND_PROVIDER:
                return "Groq Compound web search is ready."
            if self.provider == _MISTRAL_CONVERSATIONS_PROVIDER:
                return "Mistral Conversations web_search is ready."
            if self.provider == _MINIMAX_CODING_PLAN_PROVIDER:
                return "MiniMax Token Plan web_search is ready."
            if self.provider == _COHERE_WEB_SEARCH_PROVIDER:
                return "Cohere hosted web-search connector is ready."
            return "web_search is ready for chat and top-level Plan/readonly sessions."

        if self.mode == _WEB_SEARCH_MODE_OFF:
            return "Enable with `alysis config set web_search_mode auto`, `native`, or `external`."

        if self.notes and self.notes[0].startswith("invalid web_search_adapter: "):
            return (
                f"Fix web_search_adapter, {_WEB_SEARCH_ADAPTER_ENV}, or {_WEB_SEARCH_PROVIDER_ENV}."
            )

        if any(" is incompatible with " in note for note in self.notes):
            return "Choose a web_search_adapter compatible with the selected web_search_mode."

        if len(self.notes) > 1 and self.notes[0].startswith("explicit adapter selected "):
            return f"The selected web_search_adapter is not ready: {self.notes[1]}"

        if not self.api_key_available:
            if self.mode == _WEB_SEARCH_MODE_NATIVE:
                return (
                    "Set the active provider key with `alysis config set-api-key`, "
                    "ALYSIS_API_KEY, or ALYSIS_WEB_SEARCH_API_KEY. Native mode "
                    "does not use Tavily or other external fallback search providers."
                )
            if self.mode == _WEB_SEARCH_MODE_EXTERNAL:
                return (
                    "Set ALYSIS_WEB_SEARCH_API_KEY or TAVILY_API_KEY for external "
                    "web_search, or install the 'ddgs' package for the keyless backend."
                )
            return (
                "Set an API key with `alysis config set-api-key`, "
                "ALYSIS_API_KEY, or set ALYSIS_WEB_SEARCH_API_KEY/TAVILY_API_KEY "
                "for provider-agnostic fallback; installing the 'ddgs' package enables "
                "keyless fallback search."
            )

        if self.mode == _WEB_SEARCH_MODE_NATIVE:
            return (
                "Use a native search-capable provider/profile (OpenAI, xAI, Anthropic, "
                "Gemini, OpenRouter, DashScope/Qwen, Kimi, Zhipu/GLM, Doubao, "
                "Perplexity, Groq, Mistral, MiniMax Token Plan, or Cohere). Native mode "
                "never falls back to Tavily."
            )
        if self.mode == _WEB_SEARCH_MODE_EXTERNAL:
            return (
                "Use an external web_search adapter: set ALYSIS_WEB_SEARCH_API_KEY or "
                "TAVILY_API_KEY for Tavily, or install the 'ddgs' package for the "
                "keyless backend."
            )

        return (
            "Use a native search-capable provider/profile (OpenAI, xAI, Anthropic, Gemini, "
            "OpenRouter, DashScope/Qwen, Kimi, Zhipu/GLM, Doubao, Perplexity, Groq, "
            "Mistral, MiniMax Token Plan, or Cohere), set "
            "ALYSIS_WEB_SEARCH_API_KEY/TAVILY_API_KEY, or install the 'ddgs' package "
            "for keyless fallback search."
        )


@dataclass(frozen=True)
class _BackendReadiness:
    provider: str
    runtime: WebSearchRuntimeConfig | None
    base_url: str | None
    model: str | None
    api_key_available: bool
    notes: tuple[str, ...]


def _append_unique_note(notes: list[str], message: str) -> None:
    normalized = str(message or "").strip()
    if normalized and normalized not in notes:
        notes.append(normalized)


def _combine_notes(*note_groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in note_groups:
        for note in group:
            _append_unique_note(merged, note)
    return tuple(merged)


def _resolve_provider_override(
    *,
    cfg: AppConfig | None,
    strict: bool = False,
) -> tuple[str | None, str | None]:
    raw_value = str(env_get(_WEB_SEARCH_ADAPTER_ENV) or "").strip().lower()
    if not raw_value:
        raw_value = str(env_get(_WEB_SEARCH_PROVIDER_ENV) or "").strip().lower()
    try:
        adapter = (
            normalize_web_search_adapter(raw_value)
            if raw_value
            else resolve_web_search_adapter(cfg)
        )
    except Exception as exc:
        message = str(exc)
        if strict:
            raise WebSearchError(message) from exc
        return None, message
    if adapter == AUTO_WEB_SEARCH_ADAPTER:
        return None, None
    if adapter in _VALID_WEB_SEARCH_PROVIDERS:
        return adapter, None
    message = (
        f"web_search_adapter must be one of: "
        f"{', '.join(item for item in WEB_SEARCH_ADAPTER_CHOICES if item != AUTO_WEB_SEARCH_ADAPTER)}"
    )
    if strict:
        raise WebSearchError(message)
    return None, message


def _resolve_runtime_base_url(
    cfg: AppConfig | None,
) -> tuple[str | None, str | None]:
    explicit_base_url = resolve_web_search_explicit_base_url(cfg)
    if explicit_base_url:
        return explicit_base_url, explicit_base_url

    candidate_base_url = resolve_web_search_base_url(cfg)
    if is_first_party_openai_base_url(candidate_base_url):
        return candidate_base_url, candidate_base_url
    return candidate_base_url, None


def _responses_auth_provider_for_base_url(
    *,
    cfg: AppConfig | None,
    base_url: str | None,
) -> str | None:
    if cfg is None:
        return None
    try:
        profile = get_active_profile(cfg)
    except Exception:  # noqa: BLE001
        return None
    provider_id = str(profile.auth_provider or "").strip()
    if not provider_id or str(profile.protocol or "").strip() != OPENAI_RESPONSES_PROTOCOL:
        return None
    try:
        from ..provider_auth import create_provider_auth

        adapter = create_provider_auth(provider_id)
    except Exception:  # noqa: BLE001
        return None
    if str(getattr(adapter, "protocol", "") or "").strip() != OPENAI_RESPONSES_PROTOCOL:
        return None
    normalized_base = str(base_url or "").strip().rstrip("/")
    adapter_base = str(getattr(adapter, "base_url", "") or "").strip().rstrip("/")
    return provider_id if normalized_base and normalized_base == adapter_base else None


def _is_dashscope_base_url(base_url: str | None) -> bool:
    normalized = str(base_url or "").strip()
    if not normalized:
        return False
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    hostname = (parsed.hostname or "").rstrip(".").lower()
    return (
        hostname == "dashscope.aliyuncs.com"
        or hostname == "dashscope-intl.aliyuncs.com"
        or hostname == "dashscope-us.aliyuncs.com"
        or hostname.endswith(".dashscope.aliyuncs.com")
        or hostname.endswith(".dashscope-intl.aliyuncs.com")
        or hostname.endswith(".dashscope-us.aliyuncs.com")
        or (hostname.endswith(".aliyuncs.com") and ".dashscope-" in f".{hostname}")
    )


def _dashscope_model_supports_web_search(model: str | None) -> bool:
    return dashscope_model_supports_web_search(model)


def _base_url_host(base_url: str | None) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return ""
    return (parsed.hostname or "").rstrip(".").lower()


def _is_host_or_subdomain(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def _is_anthropic_base_url(base_url: str | None) -> bool:
    return _is_host_or_subdomain(_base_url_host(base_url), "api.anthropic.com")


def _is_gemini_base_url(base_url: str | None) -> bool:
    return _is_host_or_subdomain(_base_url_host(base_url), "generativelanguage.googleapis.com")


def _is_xai_base_url(base_url: str | None) -> bool:
    return _is_host_or_subdomain(_base_url_host(base_url), "api.x.ai")


def _is_openrouter_base_url(base_url: str | None) -> bool:
    # NOTE: the Alysis Code hosted proxy (`…supabase.co/functions/v1/llm`) used to
    # be classified as OpenRouter while it forwarded to OpenRouter/Xiaomi during
    # the retired MiMo trial. It now forwards to DeepSeek (no native web
    # search), so no special-case applies anymore.
    return _is_host_or_subdomain(_base_url_host(base_url), "openrouter.ai")


def _is_perplexity_base_url(base_url: str | None) -> bool:
    return _is_host_or_subdomain(_base_url_host(base_url), "api.perplexity.ai")


def _is_groq_base_url(base_url: str | None) -> bool:
    return _is_host_or_subdomain(_base_url_host(base_url), "api.groq.com")


def _is_mistral_base_url(base_url: str | None) -> bool:
    return _is_host_or_subdomain(_base_url_host(base_url), "api.mistral.ai")


def _is_minimax_base_url(base_url: str | None) -> bool:
    host = _base_url_host(base_url)
    return _is_host_or_subdomain(host, "api.minimax.io") or _is_host_or_subdomain(
        host,
        "api.minimaxi.com",
    )


def _is_cohere_base_url(base_url: str | None) -> bool:
    host = _base_url_host(base_url)
    return _is_host_or_subdomain(host, "api.cohere.ai") or _is_host_or_subdomain(
        host,
        "api.cohere.com",
    )


def _is_moonshot_base_url(base_url: str | None) -> bool:
    host = _base_url_host(base_url)
    return _is_host_or_subdomain(host, "api.moonshot.cn") or _is_host_or_subdomain(
        host,
        "api.moonshot.ai",
    )


def _is_zhipu_base_url(base_url: str | None) -> bool:
    return _is_host_or_subdomain(_base_url_host(base_url), "open.bigmodel.cn")


def _is_volcengine_base_url(base_url: str | None) -> bool:
    host = _base_url_host(base_url)
    return _is_host_or_subdomain(host, "volces.com") or _is_host_or_subdomain(
        host,
        "volcengine.com",
    )


def _resolve_search_model_or_default(cfg: AppConfig | None, default_model: str) -> str | None:
    model = resolve_web_search_model(cfg)
    if model:
        return model
    return default_model or None


def _groq_search_model(cfg: AppConfig | None) -> str | None:
    model = resolve_web_search_model(cfg)
    if not model:
        return "groq/compound-mini"
    normalized = model.strip().lower()
    if normalized in {"groq/compound", "groq/compound-mini"}:
        return model
    explicit = str(getattr(cfg, "web_search_model", "") or "").strip()
    if explicit:
        return model
    return "groq/compound-mini"


def _resolve_tavily_api_key(cfg: AppConfig | None) -> str | None:
    value = str(env_get("TAVILY_API_KEY") or "").strip()
    if value:
        return value
    return resolve_web_search_api_key(cfg)


def _validate_query(raw_query: Any) -> str:
    query = str(raw_query or "").strip()
    if not query:
        raise WebSearchError("query must be a non-empty string.", recoverable=True)
    return query


def _validate_allowed_domains(raw_allowed_domains: Any) -> list[str] | None:
    if raw_allowed_domains is None:
        return None
    if not isinstance(raw_allowed_domains, list):
        raise WebSearchError(
            "allowed_domains must be an array of non-empty domain strings.",
            recoverable=True,
        )
    cleaned: list[str] = []
    for item in raw_allowed_domains:
        domain = str(item or "").strip().lower()
        if not domain:
            raise WebSearchError(
                "allowed_domains must contain only non-empty domain strings.",
                recoverable=True,
            )
        cleaned.append(domain)
    return cleaned


def _validate_max_sources(raw_max_sources: Any) -> int:
    try:
        max_sources = int(raw_max_sources if raw_max_sources is not None else 8)
    except (TypeError, ValueError) as e:
        raise WebSearchError(
            "max_sources must be an integer between 1 and 20.",
            recoverable=True,
        ) from e
    if max_sources < 1 or max_sources > 20:
        raise WebSearchError("max_sources must be an integer between 1 and 20.", recoverable=True)
    return max_sources


def _validate_external_web_access(raw_external_web_access: Any) -> bool:
    if raw_external_web_access is None:
        return True
    if not isinstance(raw_external_web_access, bool):
        raise WebSearchError("external_web_access must be a boolean.", recoverable=True)
    return raw_external_web_access


def _enforce_external_web_access_contract(*, provider: str, external_web_access: bool) -> None:
    if external_web_access or provider == _OPENAI_RESPONSES_PROVIDER:
        return
    raise WebSearchError(
        "external_web_access=false is supported only by the openai_responses web_search backend; "
        f"{provider} always uses external web access.",
        recoverable=True,
    )


def _resolve_openai_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    base_url, registration_base_url = _resolve_runtime_base_url(cfg)
    model = resolve_web_search_model(cfg)
    resolved_api_key = resolve_web_search_api_key(cfg, api_key_fallback=api_key)
    auth_provider = _responses_auth_provider_for_base_url(cfg=cfg, base_url=base_url)
    subscription_selection_ready = True
    if auth_provider:
        registration_base_url = base_url
        try:
            profile = get_active_profile(cfg)  # type: ignore[arg-type]
            model = profile.default_model
            subscription_selection_ready = active_subscription_selection_ready(cfg)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            subscription_selection_ready = False
    notes: list[str] = []
    timeout_s: float | None = None

    try:
        timeout_s = resolve_web_search_timeout_s(cfg)
    except ConfigError as e:
        _append_unique_note(notes, str(e))

    if not base_url:
        _append_unique_note(
            notes,
            "missing OpenAI search base URL: set web_search_base_url or use a first-party OpenAI base_url",
        )
    elif not registration_base_url:
        _append_unique_note(
            notes,
            "OpenAI auto readiness requires explicit web_search_base_url or a first-party OpenAI base_url",
        )
    if not model:
        _append_unique_note(
            notes,
            "missing model: set model or web_search_model (ALYSIS_WEB_SEARCH_MODEL is an advanced override)",
        )
    if not resolved_api_key and not auth_provider:
        _append_unique_note(
            notes,
            "missing API key: set ALYSIS_API_KEY or ALYSIS_WEB_SEARCH_API_KEY",
        )
    if auth_provider and not subscription_selection_ready:
        _append_unique_note(
            notes,
            "choose the subscription model and reasoning effort in /config → Default Model",
        )

    runtime: WebSearchRuntimeConfig | None = None
    credentials_available = bool(resolved_api_key or auth_provider)
    if (
        registration_base_url
        and model
        and credentials_available
        and timeout_s is not None
        and subscription_selection_ready
    ):
        runtime = WebSearchRuntimeConfig(
            provider=_OPENAI_RESPONSES_PROVIDER,
            base_url=registration_base_url,
            api_key=resolved_api_key or "",
            model=model,
            timeout_s=timeout_s,
            auth_provider=auth_provider,
        )

    return _BackendReadiness(
        provider=_OPENAI_RESPONSES_PROVIDER,
        runtime=runtime,
        base_url=base_url or None,
        model=model or None,
        api_key_available=credentials_available,
        notes=tuple(notes),
    )


def _resolve_dashscope_chat_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    base_url = resolve_web_search_base_url(cfg)
    model = resolve_web_search_model(cfg)
    resolved_api_key = resolve_web_search_api_key(cfg, api_key_fallback=api_key)
    notes: list[str] = []
    timeout_s: float | None = None

    try:
        timeout_s = resolve_web_search_timeout_s(cfg)
    except ConfigError as e:
        _append_unique_note(notes, str(e))

    if not base_url:
        _append_unique_note(notes, "DashScope chat search requires a DashScope base_url")
    elif not _is_dashscope_base_url(base_url):
        _append_unique_note(notes, "DashScope chat search requires a DashScope base_url")
    if not model:
        _append_unique_note(
            notes,
            "missing model: set model or web_search_model (ALYSIS_WEB_SEARCH_MODEL is an advanced override)",
        )
    elif not _dashscope_model_supports_web_search(model):
        _append_unique_note(
            notes,
            "DashScope web search requires a supported Qwen model such as qwen3.7-plus, "
            "qwen3.6-flash, or qwen3.5-plus",
        )
    if not resolved_api_key:
        _append_unique_note(
            notes,
            "missing API key: set ALYSIS_API_KEY or ALYSIS_WEB_SEARCH_API_KEY",
        )

    runtime: WebSearchRuntimeConfig | None = None
    if (
        base_url
        and _is_dashscope_base_url(base_url)
        and model
        and _dashscope_model_supports_web_search(model)
        and resolved_api_key
        and timeout_s is not None
    ):
        runtime = WebSearchRuntimeConfig(
            provider=_DASHSCOPE_CHAT_PROVIDER,
            base_url=base_url,
            api_key=resolved_api_key,
            model=model,
            timeout_s=timeout_s,
        )

    return _BackendReadiness(
        provider=_DASHSCOPE_CHAT_PROVIDER,
        runtime=runtime,
        base_url=base_url or None,
        model=model or None,
        api_key_available=bool(resolved_api_key),
        notes=tuple(notes),
    )


def _resolve_native_provider_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
    provider: str,
    base_url_label: str,
    base_url_predicate: Callable[[str | None], bool],
    default_model: str = "",
    model_resolver: Callable[[AppConfig | None], str | None] | None = None,
    model_validator: Callable[[str | None], bool] | None = None,
    model_note: str | None = None,
    min_timeout_s: float | None = None,
) -> _BackendReadiness:
    base_url = resolve_web_search_base_url(cfg)
    model = (
        model_resolver(cfg)
        if model_resolver is not None
        else _resolve_search_model_or_default(
            cfg,
            default_model,
        )
    )
    resolved_api_key = resolve_web_search_api_key(cfg, api_key_fallback=api_key)
    notes: list[str] = []
    timeout_s: float | None = None

    try:
        timeout_s = resolve_web_search_timeout_s(cfg)
    except ConfigError as e:
        _append_unique_note(notes, str(e))

    if timeout_s is not None and min_timeout_s is not None and timeout_s < min_timeout_s:
        timeout_s = min_timeout_s

    if not base_url:
        _append_unique_note(
            notes, f"{base_url_label} web search requires {base_url_label} base_url"
        )
    elif not base_url_predicate(base_url):
        _append_unique_note(
            notes, f"{base_url_label} web search requires {base_url_label} base_url"
        )
    if not model:
        _append_unique_note(
            notes,
            "missing model: set model, web_search_model, or profile.web_search_model",
        )
    elif model_validator is not None and not model_validator(model):
        _append_unique_note(notes, model_note or f"{base_url_label} model is not search-capable")
    if not resolved_api_key:
        _append_unique_note(
            notes,
            "missing API key: set ALYSIS_API_KEY or ALYSIS_WEB_SEARCH_API_KEY",
        )

    runtime: WebSearchRuntimeConfig | None = None
    if (
        base_url
        and base_url_predicate(base_url)
        and model
        and (model_validator is None or model_validator(model))
        and resolved_api_key
        and timeout_s is not None
    ):
        runtime = WebSearchRuntimeConfig(
            provider=provider,
            base_url=base_url,
            api_key=resolved_api_key,
            model=model,
            timeout_s=timeout_s,
        )

    return _BackendReadiness(
        provider=provider,
        runtime=runtime,
        base_url=base_url or None,
        model=model or None,
        api_key_available=bool(resolved_api_key),
        notes=tuple(notes),
    )


def _resolve_xai_responses_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_XAI_RESPONSES_PROVIDER,
        base_url_label="xAI",
        base_url_predicate=_is_xai_base_url,
    )


def _resolve_anthropic_messages_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_ANTHROPIC_MESSAGES_PROVIDER,
        base_url_label="Anthropic",
        base_url_predicate=_is_anthropic_base_url,
    )


def _resolve_gemini_grounding_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_GEMINI_GROUNDING_PROVIDER,
        base_url_label="Gemini",
        base_url_predicate=_is_gemini_base_url,
    )


def _resolve_openrouter_web_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_OPENROUTER_WEB_PROVIDER,
        base_url_label="OpenRouter",
        base_url_predicate=_is_openrouter_base_url,
        default_model="openrouter/auto",
        min_timeout_s=_OPENROUTER_WEB_MIN_TIMEOUT_S,
    )


def _resolve_perplexity_sonar_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_PERPLEXITY_SONAR_PROVIDER,
        base_url_label="Perplexity",
        base_url_predicate=_is_perplexity_base_url,
        default_model="sonar",
    )


def _resolve_groq_compound_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_GROQ_COMPOUND_PROVIDER,
        base_url_label="Groq",
        base_url_predicate=_is_groq_base_url,
        model_resolver=_groq_search_model,
        model_validator=lambda model: (
            str(model or "").strip().lower() in {"groq/compound", "groq/compound-mini"}
        ),
        model_note="Groq web search requires groq/compound or groq/compound-mini",
    )


def _resolve_mistral_conversations_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_MISTRAL_CONVERSATIONS_PROVIDER,
        base_url_label="Mistral",
        base_url_predicate=_is_mistral_base_url,
        default_model="mistral-medium-latest",
    )


def _resolve_minimax_coding_plan_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_MINIMAX_CODING_PLAN_PROVIDER,
        base_url_label="MiniMax",
        base_url_predicate=_is_minimax_base_url,
        default_model="MiniMax-M2.7",
    )


def _resolve_cohere_web_search_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_COHERE_WEB_SEARCH_PROVIDER,
        base_url_label="Cohere",
        base_url_predicate=_is_cohere_base_url,
        default_model="command-a-plus-05-2026",
    )


def _resolve_moonshot_kimi_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_MOONSHOT_KIMI_PROVIDER,
        base_url_label="Moonshot/Kimi",
        base_url_predicate=_is_moonshot_base_url,
        default_model="kimi-k2.6",
    )


def _resolve_zhipu_web_search_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_ZHIPU_WEB_SEARCH_PROVIDER,
        base_url_label="Zhipu/GLM",
        base_url_predicate=_is_zhipu_base_url,
        default_model="glm-4.6",
    )


def _resolve_volcengine_web_search_readiness(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> _BackendReadiness:
    return _resolve_native_provider_readiness(
        cfg=cfg,
        api_key=api_key,
        provider=_VOLCENGINE_WEB_SEARCH_PROVIDER,
        base_url_label="Volcengine/Doubao",
        base_url_predicate=_is_volcengine_base_url,
        default_model="doubao-seed-1-6-250615",
    )


def _resolve_tavily_readiness(*, cfg: AppConfig | None) -> _BackendReadiness:
    notes: list[str] = []
    timeout_s: float | None = None
    resolved_api_key = _resolve_tavily_api_key(cfg)

    try:
        timeout_s = resolve_web_search_timeout_s(cfg)
    except ConfigError as e:
        _append_unique_note(notes, str(e))

    if not resolved_api_key:
        _append_unique_note(
            notes,
            "missing TAVILY_API_KEY or ALYSIS_WEB_SEARCH_API_KEY for Tavily search",
        )

    runtime: WebSearchRuntimeConfig | None = None
    if resolved_api_key and timeout_s is not None:
        runtime = WebSearchRuntimeConfig(
            provider=_TAVILY_PROVIDER,
            base_url=None,
            api_key=resolved_api_key,
            model=None,
            timeout_s=timeout_s,
        )

    return _BackendReadiness(
        provider=_TAVILY_PROVIDER,
        runtime=runtime,
        base_url=None,
        model=None,
        api_key_available=bool(resolved_api_key),
        notes=tuple(notes),
    )


def _resolve_ddgs_readiness(*, cfg: AppConfig | None) -> _BackendReadiness:
    notes: list[str] = []
    timeout_s: float | None = None

    try:
        timeout_s = resolve_web_search_timeout_s(cfg)
    except ConfigError as e:
        _append_unique_note(notes, str(e))

    enabled = keyless_web_search_enabled()
    package_available = ddgs_package_available()
    if not enabled:
        _append_unique_note(
            notes,
            f"keyless web search is disabled ({KEYLESS_WEB_SEARCH_ENV}=0)",
        )
    elif not package_available:
        _append_unique_note(
            notes,
            "keyless web search requires the 'ddgs' package (pip install ddgs)",
        )

    runtime: WebSearchRuntimeConfig | None = None
    if enabled and package_available and timeout_s is not None:
        runtime = WebSearchRuntimeConfig(
            provider=_DDGS_PROVIDER,
            base_url=None,
            api_key="",
            model=None,
            timeout_s=timeout_s,
        )

    return _BackendReadiness(
        provider=_DDGS_PROVIDER,
        runtime=runtime,
        base_url=None,
        model=None,
        api_key_available=runtime is not None,
        notes=tuple(notes),
    )


def _readiness_order() -> tuple[str, ...]:
    return (
        _OPENAI_RESPONSES_PROVIDER,
        _XAI_RESPONSES_PROVIDER,
        _ANTHROPIC_MESSAGES_PROVIDER,
        _GEMINI_GROUNDING_PROVIDER,
        _OPENROUTER_WEB_PROVIDER,
        _DASHSCOPE_CHAT_PROVIDER,
        _MOONSHOT_KIMI_PROVIDER,
        _ZHIPU_WEB_SEARCH_PROVIDER,
        _VOLCENGINE_WEB_SEARCH_PROVIDER,
        _PERPLEXITY_SONAR_PROVIDER,
        _GROQ_COMPOUND_PROVIDER,
        _MISTRAL_CONVERSATIONS_PROVIDER,
        _MINIMAX_CODING_PLAN_PROVIDER,
        _COHERE_WEB_SEARCH_PROVIDER,
        _TAVILY_PROVIDER,
        _DDGS_PROVIDER,
    )


def _readiness_order_for_mode(mode: str) -> tuple[str, ...]:
    if mode == _WEB_SEARCH_MODE_NATIVE:
        return tuple(
            provider for provider in _readiness_order() if provider in NATIVE_WEB_SEARCH_ADAPTERS
        )
    if mode == _WEB_SEARCH_MODE_EXTERNAL:
        return tuple(
            provider for provider in _readiness_order() if provider in EXTERNAL_WEB_SEARCH_ADAPTERS
        )
    return _readiness_order()


def _adapter_mode_incompatibility(*, mode: str, provider: str) -> str | None:
    if mode == _WEB_SEARCH_MODE_NATIVE and web_search_adapter_is_external(provider):
        return (
            f"web_search_mode=native is incompatible with web_search_adapter={provider}: "
            "native mode allows only provider-hosted/native web search adapters and never "
            "uses Tavily or other external search providers."
        )
    if mode == _WEB_SEARCH_MODE_EXTERNAL and web_search_adapter_is_native(provider):
        return (
            f"web_search_mode=external is incompatible with web_search_adapter={provider}: "
            "external mode allows only external search providers such as Tavily and does "
            "not use provider-hosted/native adapters."
        )
    return None


def _resolve_backend_readiness_map(
    *,
    cfg: AppConfig | None,
    api_key: str | None,
) -> dict[str, _BackendReadiness]:
    return {
        _OPENAI_RESPONSES_PROVIDER: _resolve_openai_readiness(cfg=cfg, api_key=api_key),
        _XAI_RESPONSES_PROVIDER: _resolve_xai_responses_readiness(cfg=cfg, api_key=api_key),
        _ANTHROPIC_MESSAGES_PROVIDER: _resolve_anthropic_messages_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _GEMINI_GROUNDING_PROVIDER: _resolve_gemini_grounding_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _OPENROUTER_WEB_PROVIDER: _resolve_openrouter_web_readiness(cfg=cfg, api_key=api_key),
        _DASHSCOPE_CHAT_PROVIDER: _resolve_dashscope_chat_readiness(cfg=cfg, api_key=api_key),
        _MOONSHOT_KIMI_PROVIDER: _resolve_moonshot_kimi_readiness(cfg=cfg, api_key=api_key),
        _ZHIPU_WEB_SEARCH_PROVIDER: _resolve_zhipu_web_search_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _VOLCENGINE_WEB_SEARCH_PROVIDER: _resolve_volcengine_web_search_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _PERPLEXITY_SONAR_PROVIDER: _resolve_perplexity_sonar_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _GROQ_COMPOUND_PROVIDER: _resolve_groq_compound_readiness(cfg=cfg, api_key=api_key),
        _MISTRAL_CONVERSATIONS_PROVIDER: _resolve_mistral_conversations_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _MINIMAX_CODING_PLAN_PROVIDER: _resolve_minimax_coding_plan_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _COHERE_WEB_SEARCH_PROVIDER: _resolve_cohere_web_search_readiness(
            cfg=cfg,
            api_key=api_key,
        ),
        _TAVILY_PROVIDER: _resolve_tavily_readiness(cfg=cfg),
        _DDGS_PROVIDER: _resolve_ddgs_readiness(cfg=cfg),
    }


def _combined_api_key_available(
    readiness: dict[str, _BackendReadiness],
    providers: tuple[str, ...] | None = None,
) -> bool:
    order = providers or _readiness_order()
    return any(readiness[provider].api_key_available for provider in order)


def _first_context_backend(
    readiness: dict[str, _BackendReadiness],
    providers: tuple[str, ...] | None = None,
) -> _BackendReadiness:
    order = providers or _readiness_order()
    for provider in order:
        item = readiness[provider]
        if item.base_url or item.model:
            return item
    if order:
        return readiness[order[0]]
    return readiness[_OPENAI_RESPONSES_PROVIDER]


def _format_runtime_context(item: _BackendReadiness) -> str:
    base_url = item.base_url or "(missing)"
    model = item.model or "(missing)"
    return f"base_url={base_url}, model={model}"


def _mode_unavailable_notes(
    *,
    mode: str,
    readiness: dict[str, _BackendReadiness],
    providers: tuple[str, ...],
) -> tuple[str, ...]:
    combined = _combine_notes(*(readiness[provider].notes for provider in providers))
    if mode == _WEB_SEARCH_MODE_NATIVE:
        context = _first_context_backend(readiness, providers)
        return _combine_notes(
            (
                "web_search_mode=native found no ready provider-hosted/native adapter for "
                f"the active provider/profile ({_format_runtime_context(context)}). "
                "Native mode never falls back to Tavily or other external search providers.",
            ),
            combined,
        )
    if mode == _WEB_SEARCH_MODE_EXTERNAL:
        return _combine_notes(
            (
                "web_search_mode=external found no ready external web search adapter. "
                "Configure ALYSIS_WEB_SEARCH_API_KEY/TAVILY_API_KEY or install the "
                "'ddgs' package for the keyless backend.",
            ),
            combined,
        )
    return combined


def _select_runtime_status(
    *,
    mode: str,
    provider_override: str | None,
    override_error: str | None,
    readiness: dict[str, _BackendReadiness],
) -> WebSearchRuntimeStatus:
    if mode == _WEB_SEARCH_MODE_OFF:
        return WebSearchRuntimeStatus(
            mode=mode,
            provider=None,
            base_url=None,
            model=None,
            api_key_available=False,
            registration_ready=False,
            notes=("disabled by policy: set web_search_mode=auto, native, or external",),
        )

    mode_providers = _readiness_order_for_mode(mode)

    if override_error:
        context = _first_context_backend(readiness, mode_providers)
        return WebSearchRuntimeStatus(
            mode=mode,
            provider=None,
            base_url=context.base_url,
            model=context.model,
            api_key_available=_combined_api_key_available(readiness, mode_providers),
            registration_ready=False,
            notes=(f"invalid web_search_adapter: {override_error}",),
        )

    if provider_override is not None:
        selected = readiness[provider_override]
        incompatibility = _adapter_mode_incompatibility(mode=mode, provider=provider_override)
        if incompatibility is not None:
            context = _first_context_backend(readiness, mode_providers)
            return WebSearchRuntimeStatus(
                mode=mode,
                provider=None,
                base_url=selected.base_url or context.base_url,
                model=selected.model or context.model,
                api_key_available=selected.api_key_available,
                registration_ready=False,
                notes=_combine_notes(
                    (f"explicit adapter selected {selected.provider}",),
                    (incompatibility,),
                ),
            )
        if selected.runtime is not None:
            return WebSearchRuntimeStatus(
                mode=mode,
                provider=selected.provider,
                base_url=selected.base_url,
                model=selected.model,
                api_key_available=selected.api_key_available,
                registration_ready=True,
                notes=(f"explicit adapter selected {selected.provider}",),
            )
        return WebSearchRuntimeStatus(
            mode=mode,
            provider=None,
            base_url=selected.base_url,
            model=selected.model,
            api_key_available=selected.api_key_available,
            registration_ready=False,
            notes=_combine_notes(
                (f"explicit adapter selected {selected.provider}",),
                selected.notes,
            ),
        )

    for provider in mode_providers:
        item = readiness[provider]
        if item.runtime is not None:
            selection_prefix = "auto" if mode == _WEB_SEARCH_MODE_AUTO else mode
            return WebSearchRuntimeStatus(
                mode=mode,
                provider=item.provider,
                base_url=item.base_url,
                model=item.model,
                api_key_available=item.api_key_available,
                registration_ready=True,
                notes=(f"{selection_prefix} selected {item.provider}",),
            )

    context = _first_context_backend(readiness, mode_providers)
    return WebSearchRuntimeStatus(
        mode=mode,
        provider=None,
        base_url=context.base_url,
        model=context.model,
        api_key_available=_combined_api_key_available(readiness, mode_providers),
        registration_ready=False,
        notes=_mode_unavailable_notes(
            mode=mode,
            readiness=readiness,
            providers=mode_providers,
        ),
    )


def resolve_web_search_runtime_status(
    *,
    cfg: AppConfig | None,
    api_key: str | None = None,
) -> WebSearchRuntimeStatus:
    mode = resolve_web_search_mode(cfg)
    provider_override, override_error = _resolve_provider_override(cfg=cfg, strict=False)
    readiness = _resolve_backend_readiness_map(cfg=cfg, api_key=api_key)
    return _select_runtime_status(
        mode=mode,
        provider_override=provider_override,
        override_error=override_error,
        readiness=readiness,
    )


def _strict_runtime_error_message(
    *,
    mode: str,
    provider_override: str | None,
    override_error: str | None,
    readiness: dict[str, _BackendReadiness],
) -> str:
    if mode == _WEB_SEARCH_MODE_OFF:
        return "web_search is disabled by policy (web_search_mode=off)."
    if override_error:
        return override_error
    mode_providers = _readiness_order_for_mode(mode)
    if provider_override is not None:
        incompatibility = _adapter_mode_incompatibility(mode=mode, provider=provider_override)
        if incompatibility is not None:
            return incompatibility
        selected = readiness[provider_override]
        reason = (
            selected.notes[0] if selected.notes else f"{provider_override} runtime is not ready"
        )
        mode_suffix = "" if mode == _WEB_SEARCH_MODE_AUTO else f" for web_search_mode={mode}"
        return f"web_search adapter {provider_override} is not ready{mode_suffix}: {reason}"
    combined = _combine_notes(*(readiness[provider].notes for provider in mode_providers))
    if mode == _WEB_SEARCH_MODE_NATIVE:
        context = _first_context_backend(readiness, mode_providers)
        details = "; ".join(combined) if combined else "no native/provider-hosted adapter is ready"
        return (
            "web_search is not available in native mode for active provider/profile "
            f"({_format_runtime_context(context)}): {details}. Native mode never falls "
            "back to Tavily or other external search providers."
        )
    if mode == _WEB_SEARCH_MODE_EXTERNAL:
        details = "; ".join(combined) if combined else "no external adapter is ready"
        return (
            f"web_search is not available in external mode: {details}. "
            "Configure ALYSIS_WEB_SEARCH_API_KEY or TAVILY_API_KEY for Tavily external search."
        )
    if combined:
        return "web_search is not available in auto mode: " + "; ".join(combined)
    return "web_search is not enabled or configured."


def resolve_web_search_runtime(
    *,
    cfg: AppConfig | None,
    api_key: str | None = None,
    strict: bool = False,
) -> WebSearchRuntimeConfig | None:
    mode = resolve_web_search_mode(cfg)
    provider_override, override_error = _resolve_provider_override(cfg=cfg, strict=False)
    readiness = _resolve_backend_readiness_map(cfg=cfg, api_key=api_key)

    if mode == _WEB_SEARCH_MODE_OFF:
        return None

    if override_error:
        if strict:
            raise WebSearchError(
                _strict_runtime_error_message(
                    mode=mode,
                    provider_override=provider_override,
                    override_error=override_error,
                    readiness=readiness,
                )
            )
        return None

    selected_runtime: WebSearchRuntimeConfig | None = None
    if provider_override is not None:
        if _adapter_mode_incompatibility(mode=mode, provider=provider_override) is None:
            selected_runtime = readiness[provider_override].runtime
    else:
        for provider in _readiness_order_for_mode(mode):
            selected_runtime = readiness[provider].runtime
            if selected_runtime is not None:
                break

    if selected_runtime is not None:
        return selected_runtime

    if strict:
        raise WebSearchError(
            _strict_runtime_error_message(
                mode=mode,
                provider_override=provider_override,
                override_error=override_error,
                readiness=readiness,
            )
        )
    return None


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def _with_web_search_observability(
    *,
    result: dict[str, Any],
    runtime: WebSearchRuntimeConfig,
    cfg: AppConfig | None,
    fallback_occurred: bool = False,
) -> dict[str, Any]:
    enriched = dict(result)
    # Host wall-clock provenance: when this search actually ran. Lets the model
    # anchor "today"/"current" reasoning to real time instead of its training
    # prior (which otherwise poisons follow-up queries with a stale year).
    enriched["retrieved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    citations = enriched.get("citations")
    sources = enriched.get("sources")
    queries = enriched.get("queries")
    citation_count = len(citations) if isinstance(citations, list) else 0
    source_count = len(sources) if isinstance(sources, list) else 0
    query_count = len(queries) if isinstance(queries, list) else 0
    provider_hosted = web_search_adapter_is_native(runtime.provider)
    chat_protocol = _observability_chat_protocol(cfg)
    enriched["protocol"] = chat_protocol or runtime.provider
    enriched["chat_protocol"] = chat_protocol
    enriched["search_protocol"] = runtime.provider
    enriched["backend_adapter"] = runtime.provider
    enriched["provider_hosted_search"] = provider_hosted
    enriched["external_search_provider"] = (
        runtime.provider if web_search_adapter_is_external(runtime.provider) else None
    )
    enriched["citation_count"] = citation_count
    enriched["source_count"] = source_count
    record_web_search_call(
        protocol=chat_protocol or runtime.provider,
        provider_key=runtime.provider,
        model=runtime.model,
        web_search_mode=resolve_web_search_mode(cfg),
        web_search_adapter=resolve_web_search_adapter(cfg),
        provider_hosted_search=provider_hosted,
        external_provider_name=enriched["external_search_provider"],
        source_count=source_count,
        citation_count=citation_count,
        query_count=query_count,
        fallback_occurred=fallback_occurred,
    )
    return enriched


def _observability_chat_protocol(cfg: AppConfig | None) -> str:
    if cfg is None:
        return ""
    try:
        profile = get_active_profile(cfg)
    except Exception:  # noqa: BLE001
        return OPENAI_COMPAT_PROTOCOL
    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    return protocol or OPENAI_COMPAT_PROTOCOL


def _openai_responses_search(
    *,
    query: str,
    runtime: WebSearchRuntimeConfig,
    cfg: AppConfig | None,
    allowed_domains: list[str] | None,
    max_sources: int,
    external_web_access: bool,
    transport: httpx.BaseTransport | None,
    client_factory: Callable[..., OpenAIResponsesClient],
    session_id: str | None,
) -> dict[str, Any]:
    provider_auth = None
    if runtime.auth_provider:
        from ..provider_auth import create_provider_auth

        provider_auth = create_provider_auth(runtime.auth_provider, transport=transport)
    client = client_factory(
        base_url=str(runtime.base_url or ""),
        api_key=runtime.api_key,
        model=str(runtime.model or ""),
        timeout_s=runtime.timeout_s,
        transport=transport,
        provider_concurrency_caps=getattr(cfg, "provider_concurrency_caps", None),
        provider_retry_settings=_web_search_provider_retry_settings(cfg),
        provider_auth=provider_auth,
        session_id=session_id,
    )

    try:
        is_xai_runtime = runtime.provider == _XAI_RESPONSES_PROVIDER
        response = client.web_search(
            query=query,
            allowed_domains=allowed_domains,
            external_web_access=None if is_xai_runtime else external_web_access,
            include_source_details=not is_xai_runtime,
        )
    except ResponsesError as e:
        raise WebSearchError(str(e)) from e

    raw_sources = [
        {
            "url": str(source.url or "").strip(),
            "title": str(source.title or "").strip(),
        }
        for source in response.sources
        if str(source.url or "").strip()
    ]
    deduped_sources = _dedupe_sources(raw_sources)
    sources_truncated = len(deduped_sources) > max_sources
    final_sources = deduped_sources[:max_sources]

    citations = [
        {
            "title": str(citation.title or "").strip(),
            "url": str(citation.url or "").strip(),
            "start_index": citation.start_index,
            "end_index": citation.end_index,
        }
        for citation in response.citations
        if str(citation.url or "").strip()
    ]

    queries: list[str] = []
    for raw_query in response.queries:
        query_text = str(raw_query or "").strip()
        if query_text:
            queries.append(query_text)

    return {
        "query": query,
        "answer": str(response.answer or "").strip(),
        "citations": citations,
        "sources": final_sources,
        "queries": queries,
        "model": response.model or runtime.model,
        "backend": runtime.provider,
        "allowed_domains": allowed_domains or [],
        "external_web_access": external_web_access,
        "response_id": response.response_id,
        "sources_truncated": sources_truncated,
    }


def web_search(
    *,
    query: str,
    cfg: AppConfig | None,
    api_key: str | None = None,
    allowed_domains: list[str] | None = None,
    max_sources: int = 8,
    external_web_access: bool = True,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    client_factory: Callable[..., OpenAIResponsesClient] = OpenAIResponsesClient,
    session_id: str | None = None,
) -> dict[str, Any]:
    # Defense-in-depth runtime guard mirroring web_fetch: the master web-tools
    # switch blocks execution even if registration gating was bypassed.
    from ..config import resolve_web_tools_enabled

    if not resolve_web_tools_enabled(cfg):
        raise WebSearchError(
            "web_search is disabled by the web-tools policy "
            "(ALYSIS_WEB_TOOLS=off / web_tools_enabled=false). "
            "No network request was performed.",
            recoverable=False,
        )
    validated_query = _validate_query(query)
    validated_allowed_domains = _validate_allowed_domains(allowed_domains)
    validated_max_sources = _validate_max_sources(max_sources)
    validated_external_web_access = _validate_external_web_access(external_web_access)

    provider_override, _override_error = _resolve_provider_override(cfg=cfg, strict=True)
    runtime = resolve_web_search_runtime(cfg=cfg, api_key=api_key, strict=True)
    if runtime is None:
        raise WebSearchError("web_search is not enabled or configured.")
    _enforce_external_web_access_contract(
        provider=runtime.provider,
        external_web_access=validated_external_web_access,
    )

    def _run_tavily(tavily_runtime: WebSearchRuntimeConfig) -> dict[str, Any]:
        return tavily_search(
            query=validated_query,
            api_key=tavily_runtime.api_key,
            max_results=validated_max_sources,
            include_domains=validated_allowed_domains,
            timeout_s=tavily_runtime.timeout_s,
            transport=transport,
            resolver=resolver,
        )

    def _run_ddgs(ddgs_runtime: WebSearchRuntimeConfig) -> dict[str, Any]:
        return ddgs_search(
            query=validated_query,
            max_results=validated_max_sources,
            include_domains=validated_allowed_domains,
            timeout_s=ddgs_runtime.timeout_s,
        )

    def _fallback_to_external_or_raise(provider: str, error: Exception) -> dict[str, Any]:
        if provider_override is not None:
            raise WebSearchError(str(error)) from error
        mode = resolve_web_search_mode(cfg)
        if mode == _WEB_SEARCH_MODE_NATIVE:
            raise WebSearchError(
                f"native web search via {provider} failed and web_search_mode="
                "native disables external fallback backends (set web_search_mode "
                f"to 'auto' or 'external' to allow them): {error}"
            ) from error
        if mode != _WEB_SEARCH_MODE_AUTO or not validated_external_web_access:
            raise WebSearchError(str(error)) from error
        failures = [f"{provider}: {error}"]
        last_error: Exception = error
        if provider != _TAVILY_PROVIDER:
            tavily_runtime = _resolve_tavily_readiness(cfg=cfg).runtime
            if tavily_runtime is not None:
                try:
                    result = _with_web_search_observability(
                        result=_run_tavily(tavily_runtime),
                        runtime=tavily_runtime,
                        cfg=cfg,
                        fallback_occurred=True,
                    )
                except TavilySearchError as tavily_error:
                    failures.append(f"tavily: {tavily_error}")
                    last_error = tavily_error
                else:
                    _record_session_fallback_preference(
                        session_id,
                        _TAVILY_PROVIDER,
                        failed_runtime_fingerprint=_web_search_runtime_fingerprint(runtime),
                    )
                    return result
        if provider != _DDGS_PROVIDER:
            ddgs_runtime = _resolve_ddgs_readiness(cfg=cfg).runtime
            if ddgs_runtime is not None:
                try:
                    result = _with_web_search_observability(
                        result=_run_ddgs(ddgs_runtime),
                        runtime=ddgs_runtime,
                        cfg=cfg,
                        fallback_occurred=True,
                    )
                except DdgsSearchError as ddgs_error:
                    failures.append(f"ddgs: {ddgs_error}")
                    last_error = ddgs_error
                else:
                    _record_session_fallback_preference(
                        session_id,
                        _DDGS_PROVIDER,
                        failed_runtime_fingerprint=_web_search_runtime_fingerprint(runtime),
                    )
                    return result
        if len(failures) == 1:
            raise WebSearchError(str(error)) from error
        raise WebSearchError(
            "web_search failed across auto backends: " + "; ".join(failures)
        ) from last_error

    preferred_fallback = _session_fallback_preference(
        session_id,
        current_runtime_fingerprint=_web_search_runtime_fingerprint(runtime),
    )
    if (
        preferred_fallback in (_TAVILY_PROVIDER, _DDGS_PROVIDER)
        and preferred_fallback != runtime.provider
        and provider_override is None
        and resolve_web_search_mode(cfg) == _WEB_SEARCH_MODE_AUTO
        and validated_external_web_access
    ):
        preferred_runtime = (
            _resolve_tavily_readiness(cfg=cfg).runtime
            if preferred_fallback == _TAVILY_PROVIDER
            else _resolve_ddgs_readiness(cfg=cfg).runtime
        )
        if preferred_runtime is None:
            _clear_session_fallback_preference(session_id)
        else:
            try:
                return _with_web_search_observability(
                    result=(
                        _run_tavily(preferred_runtime)
                        if preferred_fallback == _TAVILY_PROVIDER
                        else _run_ddgs(preferred_runtime)
                    ),
                    runtime=preferred_runtime,
                    cfg=cfg,
                    fallback_occurred=True,
                )
            except (TavilySearchError, DdgsSearchError):
                # The remembered fallback stopped working; forget it and give
                # the configured backend another chance on this call.
                _clear_session_fallback_preference(session_id)

    if runtime.provider == _TAVILY_PROVIDER:
        try:
            return _with_web_search_observability(
                result=_run_tavily(runtime),
                runtime=runtime,
                cfg=cfg,
            )
        except TavilySearchError as e:
            return _fallback_to_external_or_raise(_TAVILY_PROVIDER, e)

    if runtime.provider == _DDGS_PROVIDER:
        try:
            return _with_web_search_observability(
                result=_run_ddgs(runtime),
                runtime=runtime,
                cfg=cfg,
            )
        except DdgsSearchError as e:
            raise WebSearchError(str(e)) from e

    if runtime.provider == _DASHSCOPE_CHAT_PROVIDER:
        try:
            return _with_web_search_observability(
                result=dashscope_chat_search(
                    query=validated_query,
                    base_url=str(runtime.base_url or ""),
                    api_key=runtime.api_key,
                    model=str(runtime.model or ""),
                    max_results=validated_max_sources,
                    include_domains=validated_allowed_domains,
                    timeout_s=runtime.timeout_s,
                    transport=transport,
                    resolver=resolver,
                    provider_concurrency_caps=getattr(cfg, "provider_concurrency_caps", None),
                    provider_retry_settings=_web_search_provider_retry_settings(cfg),
                ),
                runtime=runtime,
                cfg=cfg,
            )
        except DashScopeChatSearchError as e:
            return _fallback_to_external_or_raise(_DASHSCOPE_CHAT_PROVIDER, e)

    provider_retry_settings = _web_search_provider_retry_settings(cfg)
    native_kwargs = {
        "query": validated_query,
        "base_url": str(runtime.base_url or ""),
        "api_key": runtime.api_key,
        "model": str(runtime.model or ""),
        "max_results": validated_max_sources,
        "allowed_domains": validated_allowed_domains,
        "timeout_s": runtime.timeout_s,
        "transport": transport,
        "resolver": resolver,
        "provider_concurrency_caps": getattr(cfg, "provider_concurrency_caps", None),
        "provider_retry_settings": provider_retry_settings,
    }

    if runtime.provider == _ANTHROPIC_MESSAGES_PROVIDER:
        try:
            return _with_web_search_observability(
                result=anthropic_messages_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_ANTHROPIC_MESSAGES_PROVIDER, e)

    if runtime.provider == _GEMINI_GROUNDING_PROVIDER:
        try:
            return _with_web_search_observability(
                result=gemini_grounding_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_GEMINI_GROUNDING_PROVIDER, e)

    if runtime.provider == _OPENROUTER_WEB_PROVIDER:
        try:
            return _with_web_search_observability(
                result=openrouter_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_OPENROUTER_WEB_PROVIDER, e)

    if runtime.provider == _MOONSHOT_KIMI_PROVIDER:
        try:
            return _with_web_search_observability(
                result=moonshot_kimi_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_MOONSHOT_KIMI_PROVIDER, e)

    if runtime.provider == _ZHIPU_WEB_SEARCH_PROVIDER:
        try:
            return _with_web_search_observability(
                result=zhipu_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_ZHIPU_WEB_SEARCH_PROVIDER, e)

    if runtime.provider == _VOLCENGINE_WEB_SEARCH_PROVIDER:
        try:
            return _with_web_search_observability(
                result=volcengine_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_VOLCENGINE_WEB_SEARCH_PROVIDER, e)

    if runtime.provider == _PERPLEXITY_SONAR_PROVIDER:
        try:
            return _with_web_search_observability(
                result=perplexity_sonar_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_PERPLEXITY_SONAR_PROVIDER, e)

    if runtime.provider == _GROQ_COMPOUND_PROVIDER:
        try:
            return _with_web_search_observability(
                result=groq_compound_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_GROQ_COMPOUND_PROVIDER, e)

    if runtime.provider == _MISTRAL_CONVERSATIONS_PROVIDER:
        try:
            return _with_web_search_observability(
                result=mistral_conversations_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_MISTRAL_CONVERSATIONS_PROVIDER, e)

    if runtime.provider == _MINIMAX_CODING_PLAN_PROVIDER:
        try:
            return _with_web_search_observability(
                result=minimax_coding_plan_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_MINIMAX_CODING_PLAN_PROVIDER, e)

    if runtime.provider == _COHERE_WEB_SEARCH_PROVIDER:
        try:
            return _with_web_search_observability(
                result=cohere_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_COHERE_WEB_SEARCH_PROVIDER, e)

    try:
        return _with_web_search_observability(
            result=_openai_responses_search(
                query=validated_query,
                runtime=runtime,
                cfg=cfg,
                allowed_domains=validated_allowed_domains,
                max_sources=validated_max_sources,
                external_web_access=validated_external_web_access,
                transport=transport,
                client_factory=client_factory,
                session_id=session_id,
            ),
            runtime=runtime,
            cfg=cfg,
        )
    except WebSearchError as openai_error:
        return _fallback_to_external_or_raise(runtime.provider, openai_error)
