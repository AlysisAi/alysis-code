from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..request_estimation import estimate_message_tokens
from .cache_capabilities import CACHE_CONTROL_FIELD

_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}
_DEFAULT_CACHE_CONTROL_MIN_TOKENS = 0


@dataclass(frozen=True)
class CacheControlApplication:
    messages: list[dict[str, Any]]
    applied: bool
    eligible: bool
    prefix_message_count: int
    prefix_estimated_tokens: int
    min_tokens: int
    reason: str

    def policy_metadata(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "used": self.applied,
            "cacheable_prefix_estimated_tokens": self.prefix_estimated_tokens,
        }


def apply_openai_compatible_cache_control_breakpoint(
    messages: list[dict[str, Any]],
    *,
    cache_policy: Mapping[str, Any] | None,
) -> CacheControlApplication:
    copied = copy.deepcopy([message for message in messages if isinstance(message, dict)])
    min_tokens = _cache_control_min_tokens(cache_policy)
    prefix_count = cacheable_prefix_message_count(copied)
    prefix_tokens = estimate_message_tokens(copied[:prefix_count])
    if CACHE_CONTROL_FIELD not in _policy_emitted_fields(cache_policy):
        return CacheControlApplication(
            messages=copied,
            applied=False,
            eligible=False,
            prefix_message_count=prefix_count,
            prefix_estimated_tokens=prefix_tokens,
            min_tokens=min_tokens,
            reason="cache_control_not_emitted",
        )
    if count_cache_control_blocks(copied) > 0:
        return CacheControlApplication(
            messages=copied,
            applied=True,
            eligible=True,
            prefix_message_count=prefix_count,
            prefix_estimated_tokens=prefix_tokens,
            min_tokens=min_tokens,
            reason="existing_cache_control_block",
        )
    if prefix_count <= 0:
        return CacheControlApplication(
            messages=copied,
            applied=False,
            eligible=False,
            prefix_message_count=prefix_count,
            prefix_estimated_tokens=prefix_tokens,
            min_tokens=min_tokens,
            reason="no_cacheable_prefix",
        )
    if prefix_tokens < min_tokens:
        return CacheControlApplication(
            messages=copied,
            applied=False,
            eligible=False,
            prefix_message_count=prefix_count,
            prefix_estimated_tokens=prefix_tokens,
            min_tokens=min_tokens,
            reason="prefix_below_min_tokens",
        )
    for index in range(prefix_count - 1, -1, -1):
        if _add_cache_control_to_message(copied[index]):
            return CacheControlApplication(
                messages=copied,
                applied=True,
                eligible=True,
                prefix_message_count=prefix_count,
                prefix_estimated_tokens=prefix_tokens,
                min_tokens=min_tokens,
                reason="cache_control_block_added",
            )
    return CacheControlApplication(
        messages=copied,
        applied=False,
        eligible=False,
        prefix_message_count=prefix_count,
        prefix_estimated_tokens=prefix_tokens,
        min_tokens=min_tokens,
        reason="no_text_block_in_prefix",
    )


def cacheable_prefix_message_count(messages: list[dict[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if str(messages[index].get("role") or "").strip().lower() == "user":
            return max(0, index)
    return max(0, len(messages) - 1)


def _is_request_payload(value: Mapping[str, Any]) -> bool:
    return isinstance(value.get("messages"), (list, tuple))


def _iter_block_carriers(blocks: Any) -> Iterator[Mapping[str, Any]]:
    if not isinstance(blocks, (list, tuple)):
        return
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        yield block
        yield from _iter_block_carriers(block.get("content"))


def _iter_cache_control_carriers(
    value: Any,
    *,
    skip_root: bool = False,
) -> Iterator[Mapping[str, Any]]:
    # Only positions where providers accept cache_control: the payload top
    # level, messages and their content blocks, system blocks, and the top
    # level of each tools[] entry. Never nested tool input schemas or other
    # structured data, where a field merely named "cache_control" is data.
    if isinstance(value, Mapping):
        if _is_request_payload(value):
            if not skip_root:
                yield value
            yield from _iter_block_carriers(value.get("messages"))
            system = value.get("system")
            if isinstance(system, (list, tuple)):
                yield from _iter_block_carriers(system)
            tools = value.get("tools")
            if isinstance(tools, (list, tuple)):
                for tool in tools:
                    if isinstance(tool, Mapping):
                        yield tool
            return
        if not skip_root:
            yield value
        yield from _iter_block_carriers(value.get("content"))
        return
    if isinstance(value, (list, tuple)):
        yield from _iter_block_carriers(value)


def count_cache_control_blocks(value: Any) -> int:
    return sum(
        1 for carrier in _iter_cache_control_carriers(value) if CACHE_CONTROL_FIELD in carrier
    )


def count_explicit_cache_control_blocks(value: Any) -> int:
    return sum(
        1
        for carrier in _iter_cache_control_carriers(value, skip_root=True)
        if CACHE_CONTROL_FIELD in carrier
    )


def explicit_cache_control_payloads(value: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for carrier in _iter_cache_control_carriers(value, skip_root=True):
        cache_control = carrier.get(CACHE_CONTROL_FIELD)
        if isinstance(cache_control, Mapping):
            payloads.append(copy.deepcopy(dict(cache_control)))
    return payloads


def strip_cache_control_blocks(value: Any) -> bool:
    stripped = False
    for carrier in _iter_cache_control_carriers(value):
        if isinstance(carrier, dict) and CACHE_CONTROL_FIELD in carrier:
            carrier.pop(CACHE_CONTROL_FIELD, None)
            stripped = True
    return stripped


def _policy_emitted_fields(policy: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(policy, Mapping):
        return ()
    raw = policy.get("emitted_fields")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _cache_control_min_tokens(policy: Mapping[str, Any] | None) -> int:
    if not isinstance(policy, Mapping):
        return _DEFAULT_CACHE_CONTROL_MIN_TOKENS
    for key in ("min_tokens", "min_cacheable_tokens"):
        try:
            value = int(policy.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return _DEFAULT_CACHE_CONTROL_MIN_TOKENS


def _add_cache_control_to_message(message: dict[str, Any]) -> bool:
    role = str(message.get("role") or "").strip().lower()
    if role == "tool":
        return False
    content = message.get("content")
    if isinstance(content, list):
        for index in range(len(content) - 1, -1, -1):
            item = content[index]
            if not _is_text_block(item):
                continue
            copied_item = dict(item)
            copied_item[CACHE_CONTROL_FIELD] = {"type": "ephemeral"}
            content[index] = copied_item
            return True
        return False
    if isinstance(content, str) and content.strip():
        message["content"] = [
            {
                "type": "text",
                "text": content,
                CACHE_CONTROL_FIELD: {"type": "ephemeral"},
            }
        ]
        return True
    return False


def _is_text_block(value: Any) -> bool:
    if not isinstance(value, Mapping) or CACHE_CONTROL_FIELD in value:
        return False
    block_type = str(value.get("type") or "text").strip().lower()
    if block_type not in _TEXT_BLOCK_TYPES:
        return False
    text = value.get("text")
    return isinstance(text, str) and bool(text.strip())
