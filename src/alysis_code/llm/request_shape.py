from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..request_estimation import (
    estimate_message_tokens,
    estimate_request_token_breakdown,
    estimate_tool_schema_tokens,
)
from .cache_capabilities import (
    CACHE_CONTROL_FIELD,
    CACHED_CONTENT_FIELD,
    OPENROUTER_SESSION_ID_FIELD,
    OPENROUTER_SESSION_ID_HEADER_FIELD,
    PROMPT_CACHE_KEY_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
)
from .cache_control_blocks import (
    cacheable_prefix_message_count,
    count_explicit_cache_control_blocks,
)

_AFFINITY_CACHE_FIELDS = frozenset(
    {
        PROMPT_CACHE_KEY_FIELD,
        OPENROUTER_SESSION_ID_FIELD,
        OPENROUTER_SESSION_ID_HEADER_FIELD,
        XAI_CONVERSATION_ID_HEADER_FIELD,
    }
)


def build_request_shape_report(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    cache_policy: Mapping[str, Any] | None,
    provider_payload: Mapping[str, Any] | None = None,
    input_mode: str = "full",
) -> dict[str, Any]:
    clean_messages = [message for message in messages if isinstance(message, dict)]
    clean_tools = [tool for tool in tools or [] if isinstance(tool, dict)]
    prefix_count = cacheable_prefix_message_count(clean_messages)
    prefix_messages = clean_messages[:prefix_count]
    prefix_tokens = estimate_message_tokens(prefix_messages)
    tool_schema_tokens = estimate_tool_schema_tokens(clean_tools)
    breakdown = estimate_request_token_breakdown(
        messages=clean_messages,
        tool_list=clean_tools,
        pinned_prefix_len=prefix_count,
    )
    total_tokens = breakdown.total_tokens
    min_tokens = _policy_min_tokens(cache_policy)
    emitted_fields = _policy_list(cache_policy, "emitted_fields")
    cache_enabled = (
        bool(cache_policy.get("enabled")) if isinstance(cache_policy, Mapping) else False
    )
    explicit_cache_control_blocks = count_explicit_cache_control_blocks(
        provider_payload or clean_messages
    )
    top_level_cache_control_present = (
        isinstance(provider_payload, Mapping) and CACHE_CONTROL_FIELD in provider_payload
    )
    cached_content_attached = _cached_content_attached(provider_payload)
    affinity_field_emitted = any(field in _AFFINITY_CACHE_FIELDS for field in emitted_fields)
    cache_material_attached = (
        top_level_cache_control_present
        or explicit_cache_control_blocks > 0
        or cached_content_attached
    )
    cache_fields_emitted = bool(cache_material_attached or affinity_field_emitted)
    cache_eligible = _cache_eligible(
        cache_enabled=cache_enabled,
        emitted_fields=emitted_fields,
        prefix_tokens=prefix_tokens,
        min_tokens=min_tokens,
    )
    token_breakdown = breakdown.to_payload()
    token_breakdown.pop("tool_schema_budget", None)
    return {
        "schema_version": 1,
        "input_mode": str(input_mode or "full").strip() or "full",
        "message_count": len(clean_messages),
        "tool_count": len(clean_tools),
        "system_message_count": _role_count(clean_messages, "system"),
        "developer_message_count": _role_count(clean_messages, "developer"),
        "user_message_count": _role_count(clean_messages, "user"),
        "assistant_message_count": _role_count(clean_messages, "assistant"),
        "tool_message_count": _role_count(clean_messages, "tool"),
        "tool_call_message_count": sum(
            1 for message in clean_messages if bool(message.get("tool_calls"))
        ),
        "content_block_count": sum(
            _content_block_count(message.get("content")) for message in clean_messages
        ),
        "cache_enabled": cache_enabled,
        "cache_eligible": cache_eligible,
        "cache_used": bool(cache_material_attached),
        "cache_fields_emitted": cache_fields_emitted,
        "top_level_cache_control_present": top_level_cache_control_present,
        "cached_content_attached": cached_content_attached,
        "affinity_field_emitted": affinity_field_emitted,
        "cache_strategy": _policy_label(cache_policy, "strategy"),
        "cache_status": _policy_label(cache_policy, "status"),
        "emitted_cache_fields": emitted_fields,
        "cache_control_block_count": explicit_cache_control_blocks,
        "explicit_cache_control_block_count": explicit_cache_control_blocks,
        "cacheable_prefix_present": prefix_count > 0,
        "cacheable_prefix_message_count": prefix_count,
        "cacheable_prefix_estimated_tokens": prefix_tokens,
        "cacheable_surface_estimated_tokens": prefix_tokens + tool_schema_tokens,
        "min_cacheable_tokens": min_tokens,
        "total_estimated_tokens": total_tokens,
        "tool_schema_share": _ratio(tool_schema_tokens, total_tokens),
        "inline_tool_transcript_share": _ratio(
            breakdown.inline_tool_transcript_tokens,
            total_tokens,
        ),
        "token_breakdown": token_breakdown,
        "risk_reasons": _risk_reasons(
            cache_enabled=cache_enabled,
            cache_eligible=cache_eligible,
            emitted_fields=emitted_fields,
            explicit_cache_control_blocks=explicit_cache_control_blocks,
            top_level_cache_control_present=top_level_cache_control_present,
            cached_content_attached=cached_content_attached,
            prefix_count=prefix_count,
            prefix_tokens=prefix_tokens,
            min_tokens=min_tokens,
            breakdown=token_breakdown,
            total_tokens=total_tokens,
        ),
    }


def _policy_label(policy: Mapping[str, Any] | None, key: str) -> str:
    if not isinstance(policy, Mapping):
        return ""
    return str(policy.get(key) or "").strip()


def _policy_list(policy: Mapping[str, Any] | None, key: str) -> list[str]:
    if not isinstance(policy, Mapping):
        return []
    raw = policy.get(key)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _policy_min_tokens(policy: Mapping[str, Any] | None) -> int:
    if not isinstance(policy, Mapping):
        return 0
    for key in ("min_tokens", "min_cacheable_tokens"):
        try:
            value = int(policy.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return 0


def _cached_content_attached(provider_payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(provider_payload, Mapping):
        return False
    return "cachedContent" in provider_payload or CACHED_CONTENT_FIELD in provider_payload


def _cache_eligible(
    *,
    cache_enabled: bool,
    emitted_fields: list[str],
    prefix_tokens: int,
    min_tokens: int,
) -> bool:
    if not cache_enabled:
        return False
    if CACHE_CONTROL_FIELD in emitted_fields or CACHED_CONTENT_FIELD in emitted_fields:
        return prefix_tokens >= min_tokens and prefix_tokens > 0
    return True


def _role_count(messages: list[dict[str, Any]], role: str) -> int:
    return sum(1 for message in messages if str(message.get("role") or "").strip().lower() == role)


def _content_block_count(content: Any) -> int:
    if isinstance(content, list):
        return len(content)
    return 1 if content is not None else 0


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(max(0, numerator) / denominator, 4)


def _risk_reasons(
    *,
    cache_enabled: bool,
    cache_eligible: bool,
    emitted_fields: list[str],
    explicit_cache_control_blocks: int,
    top_level_cache_control_present: bool,
    cached_content_attached: bool,
    prefix_count: int,
    prefix_tokens: int,
    min_tokens: int,
    breakdown: Mapping[str, Any],
    total_tokens: int,
) -> list[str]:
    reasons: list[str] = []
    if not cache_enabled:
        reasons.append("cache_disabled_or_unavailable")
    if cache_enabled and not emitted_fields:
        reasons.append("no_emitted_cache_fields")
    if (
        CACHE_CONTROL_FIELD in emitted_fields
        and explicit_cache_control_blocks <= 0
        and not top_level_cache_control_present
    ):
        reasons.append("cache_control_blocks_absent")
    if CACHED_CONTENT_FIELD in emitted_fields and cache_eligible and not cached_content_attached:
        reasons.append("cached_content_absent")
    if prefix_count <= 0:
        reasons.append("no_cacheable_prefix")
    if prefix_tokens < min_tokens:
        reasons.append("prefix_below_min_tokens")
    if cache_enabled and not cache_eligible:
        reasons.append("cache_not_eligible")
    if _ratio(int(breakdown.get("tool_schema_tokens") or 0), total_tokens) >= 0.25:
        reasons.append("large_tool_schema_share")
    if _ratio(int(breakdown.get("inline_tool_transcript_tokens") or 0), total_tokens) >= 0.25:
        reasons.append("large_inline_tool_transcript_share")
    return list(dict.fromkeys(reasons))
