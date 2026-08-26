from __future__ import annotations

from urllib.parse import urlsplit

_ALYSIS_TRIAL_PROXY_PATH = "/functions/v1/llm"
_ZAI_CODING_PLAN_PATH = "/api/coding/paas/v4"


def known_provider_key_from_base_url(base_url: str | None) -> str | None:
    """Classify provider-owned endpoints without guessing from arbitrary hosts.

    Callers deliberately retain their own fallback policies for unknown URLs.
    This function is only the shared truth table for provider endpoints whose
    host (and, where required, path) identifies a documented transport surface.
    """

    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").casefold()
    path = (parsed.path or "").casefold()
    if not host:
        return None

    # The Alysis Code hosted proxy (the `llm` Supabase Edge Function) forwards to
    # DeepSeek upstream, so provider-shaped behavior (limits, capabilities)
    # mirrors DeepSeek's. (In the retired MiMo-trial era the same path
    # forwarded to OpenRouter; that service no longer exists.)
    if _ALYSIS_TRIAL_PROXY_PATH in path and (
        host == "supabase.co" or host.endswith(".supabase.co")
    ):
        return "deepseek"
    # Reserved hostnames for a future dedicated gateway box — also DeepSeek.
    # Both spellings stay recognised: the pre-rebrand host may still be served,
    # and a config written against it must not start misclassifying.
    if host in {"api.sylliptor.alysisai.com", "api.alysiscode.com"}:
        return "deepseek"
    if "dashscope" in host:
        return "qwen"
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter"
    if host == "api.openai.com":
        return "openai"
    if (
        host.endswith(".openai.azure.com")
        or host.endswith(".cognitiveservices.azure.com")
        or host.endswith(".services.ai.azure.com")
    ):
        return "azure"
    if host == "api.deepseek.com" or host.endswith(".deepseek.com"):
        return "deepseek"
    if host == "integrate.api.nvidia.com":
        return "nvidia"
    if host == "api.z.ai" and (
        path == _ZAI_CODING_PLAN_PATH or path.startswith(f"{_ZAI_CODING_PLAN_PATH}/")
    ):
        return "zai_coding_plan"
    if host == "generativelanguage.googleapis.com":
        return "gemini"
    if host == "api.mistral.ai" or host.endswith(".mistral.ai"):
        return "mistral"
    if host in {"api.moonshot.ai", "api.moonshot.cn"}:
        return "moonshot"
    if host == "api.kimi.com":
        return "kimi-code"
    if host == "api.x.ai" or host == "x.ai" or host.endswith(".x.ai"):
        return "xai"
    return None


__all__ = ["known_provider_key_from_base_url"]
