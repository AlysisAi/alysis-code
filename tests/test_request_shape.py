from __future__ import annotations

import json

from alysis_code.llm.cache_capabilities import CACHE_CONTROL_FIELD
from alysis_code.llm.request_shape import build_request_shape_report


def test_request_shape_reports_safe_cacheable_prefix_metrics() -> None:
    report = build_request_shape_report(
        messages=[
            {"role": "system", "content": "large private system prompt"},
            {"role": "user", "content": "current private request"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_secret",
                    "description": "private tool description",
                    "parameters": {"type": "object"},
                },
            }
        ],
        cache_policy={
            "enabled": True,
            "status": "enabled",
            "strategy": "qwen_cache_control_blocks",
            "emitted_fields": [CACHE_CONTROL_FIELD],
            "min_tokens": 1,
        },
        provider_payload={
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "large private system prompt",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "user", "content": "current private request"},
            ],
        },
    )

    assert report["message_count"] == 2
    assert report["tool_count"] == 1
    assert report["cacheable_prefix_message_count"] == 1
    assert report["cacheable_prefix_estimated_tokens"] > 0
    assert report["cache_control_block_count"] == 1
    assert report["explicit_cache_control_block_count"] == 1
    assert report["top_level_cache_control_present"] is False
    assert report["cached_content_attached"] is False
    assert report["affinity_field_emitted"] is False
    assert report["cache_fields_emitted"] is True
    assert report["cache_eligible"] is True
    rendered = json.dumps(report, sort_keys=True)
    assert "large private system prompt" not in rendered
    assert "current private request" not in rendered
    assert "private tool description" not in rendered


def test_request_shape_flags_missing_cacheable_prefix() -> None:
    report = build_request_shape_report(
        messages=[{"role": "user", "content": "current private request"}],
        tools=None,
        cache_policy={
            "enabled": True,
            "status": "enabled",
            "strategy": "qwen_cache_control_blocks",
            "emitted_fields": [CACHE_CONTROL_FIELD],
            "min_tokens": 1,
        },
    )

    assert report["cacheable_prefix_message_count"] == 0
    assert report["cache_eligible"] is False
    assert "no_cacheable_prefix" in report["risk_reasons"]
    assert "cache_control_blocks_absent" in report["risk_reasons"]


def test_request_shape_separates_top_level_cache_control_from_explicit_blocks() -> None:
    report = build_request_shape_report(
        messages=[
            {"role": "system", "content": "stable prompt"},
            {"role": "user", "content": "current request"},
        ],
        tools=None,
        cache_policy={
            "enabled": True,
            "status": "enabled",
            "strategy": "anthropic_cache_control",
            "emitted_fields": [CACHE_CONTROL_FIELD],
            "min_tokens": 1,
        },
        provider_payload={
            "cache_control": {"type": "ephemeral"},
            "messages": [{"role": "user", "content": "current request"}],
        },
    )

    assert report["cache_used"] is True
    assert report["top_level_cache_control_present"] is True
    assert report["cache_control_block_count"] == 0
    assert report["explicit_cache_control_block_count"] == 0


def test_request_shape_reports_cached_content_attachment_separately() -> None:
    report = build_request_shape_report(
        messages=[
            {"role": "system", "content": "stable prompt"},
            {"role": "user", "content": "current request"},
        ],
        tools=None,
        cache_policy={
            "enabled": True,
            "status": "enabled",
            "strategy": "gemini_explicit_cached_content",
            "emitted_fields": ["cached_content"],
            "min_tokens": 1,
        },
        provider_payload={
            "cachedContent": "cachedContents/cache_1",
            "contents": [{"role": "user", "parts": [{"text": "current request"}]}],
        },
    )

    assert report["cache_used"] is True
    assert report["cached_content_attached"] is True
    assert report["top_level_cache_control_present"] is False
    assert report["explicit_cache_control_block_count"] == 0
