from __future__ import annotations

from alysis_code.compaction.conversation_compactor import (
    _cache_aware_compaction_decision,
    _cache_prefix_compaction_shape,
)
from alysis_code.compaction.settings import CompactionSettings
from alysis_code.request_estimation import (
    RequestTokenBreakdown,
    estimate_message_tokens,
    estimate_tool_schema_tokens,
)


def test_cache_aware_compaction_triggers_earlier_for_risky_request_shape() -> None:
    settings = CompactionSettings(
        trigger_ratio=0.90,
        cache_aware_min_trigger_ratio=0.75,
    )
    breakdown = RequestTokenBreakdown(
        bootstrap_prompt_tokens=100,
        tool_schema_tokens=250,
        live_conversation_history_tokens=200,
        inline_tool_transcript_tokens=450,
    )

    decision = _cache_aware_compaction_decision(
        settings=settings,
        request_breakdown=breakdown,
        calibration={
            "records": 5,
            "prompt_estimate_error_ratio_p90": 1.30,
            "cache_hit_ratio": 0.0,
        },
    )

    assert decision.calibrated_used_tokens == 1300
    assert decision.adjusted_trigger_ratio < settings.trigger_ratio
    assert decision.adjusted_trigger_ratio >= settings.cache_aware_min_trigger_ratio
    assert set(decision.reasons) == {
        "provider_estimate_undercount",
        "low_recent_cache_hit_ratio",
        "large_inline_tool_transcript",
        "large_tool_schema_share",
    }


def test_cache_aware_compaction_preserves_scoped_provider_gap_above_legacy_cap() -> None:
    breakdown = RequestTokenBreakdown(live_conversation_history_tokens=1000)

    decision = _cache_aware_compaction_decision(
        settings=CompactionSettings(),
        request_breakdown=breakdown,
        calibration={
            "records": 8,
            "prompt_estimate_error_ratio_p90": 1.65,
        },
    )

    assert decision.calibrated_used_tokens == 1650
    assert "provider_estimate_undercount" in decision.reasons


def test_cache_aware_floor_never_raises_user_trigger_below_floor() -> None:
    settings = CompactionSettings(
        trigger_ratio=0.60,
        target_ratio=0.50,
        cache_aware_min_trigger_ratio=0.72,
    )
    breakdown = RequestTokenBreakdown(
        bootstrap_prompt_tokens=100,
        live_conversation_history_tokens=900,
    )

    decision = _cache_aware_compaction_decision(
        settings=settings,
        request_breakdown=breakdown,
        calibration={},
    )

    assert decision.adjusted_trigger_ratio == settings.trigger_ratio


def test_cache_aware_deductions_still_floor_at_user_trigger_below_floor() -> None:
    settings = CompactionSettings(
        trigger_ratio=0.60,
        target_ratio=0.50,
        cache_aware_min_trigger_ratio=0.72,
    )
    breakdown = RequestTokenBreakdown(
        bootstrap_prompt_tokens=100,
        tool_schema_tokens=250,
        live_conversation_history_tokens=200,
        inline_tool_transcript_tokens=450,
    )

    decision = _cache_aware_compaction_decision(
        settings=settings,
        request_breakdown=breakdown,
        calibration={
            "records": 5,
            "prompt_estimate_error_ratio_p90": 1.30,
            "cache_hit_ratio": 0.0,
        },
    )

    assert decision.reasons
    assert decision.adjusted_trigger_ratio == settings.trigger_ratio


def test_cache_prefix_shape_protects_prefix_when_dynamic_suffix_is_large() -> None:
    settings = CompactionSettings(cache_aware_compaction=True)
    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "large dynamic output " + ("x" * 4000)},
    ]
    breakdown = RequestTokenBreakdown(
        bootstrap_prompt_tokens=10,
        live_conversation_history_tokens=5000,
    )

    shape = _cache_prefix_compaction_shape(
        settings=settings,
        messages=messages,
        tool_list=[],
        request_breakdown=breakdown,
        pinned_prefix_len=1,
        cache_policy={"enabled": True, "status": "enabled", "min_tokens": 1},
    )
    decision = _cache_aware_compaction_decision(
        settings=settings,
        request_breakdown=breakdown,
        calibration={},
        prefix_shape=shape,
    )

    assert shape.stable_prefix_message_count == 3
    assert shape.protected_prefix_message_count == 3
    assert shape.cacheable_prefix_preserved is True
    assert "cacheable_prefix_protected" in shape.reasons
    assert "large_dynamic_suffix" in decision.reasons
    assert decision.adjusted_trigger_ratio < settings.trigger_ratio


def test_cache_prefix_shape_allows_compaction_when_suffix_is_too_small() -> None:
    settings = CompactionSettings(cache_aware_compaction=True)
    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer " + ("x" * 4000)},
        {"role": "user", "content": "current request"},
    ]
    breakdown = RequestTokenBreakdown(
        bootstrap_prompt_tokens=10,
        live_conversation_history_tokens=1,
    )

    shape = _cache_prefix_compaction_shape(
        settings=settings,
        messages=messages,
        tool_list=[],
        request_breakdown=breakdown,
        pinned_prefix_len=1,
        cache_policy={"enabled": True, "status": "enabled", "min_tokens": 1},
    )

    assert shape.stable_prefix_message_count == 3
    assert shape.protected_prefix_message_count == 1
    assert shape.cacheable_prefix_preserved is False
    assert "cacheable_prefix_compaction_tradeoff" in shape.reasons


def test_cache_prefix_shape_skips_protection_when_cache_policy_disabled() -> None:
    settings = CompactionSettings(cache_aware_compaction=True)
    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "large dynamic output " + ("x" * 4000)},
    ]
    breakdown = RequestTokenBreakdown(
        bootstrap_prompt_tokens=10,
        live_conversation_history_tokens=5000,
    )

    shape = _cache_prefix_compaction_shape(
        settings=settings,
        messages=messages,
        tool_list=[],
        request_breakdown=breakdown,
        pinned_prefix_len=1,
        cache_policy={"enabled": False, "status": "disabled", "min_tokens": 1},
    )

    assert shape.protected_prefix_message_count == 1
    assert shape.cacheable_prefix_preserved is False
    assert "cache_disabled_or_unavailable" in shape.reasons


def test_cache_prefix_shape_does_not_use_tool_schema_to_satisfy_min_tokens() -> None:
    settings = CompactionSettings(cache_aware_compaction=True)
    messages = [
        {"role": "system", "content": "small"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "large dynamic output " + ("x" * 4000)},
    ]
    tool_list = [
        {
            "type": "function",
            "function": {
                "name": "large_schema",
                "description": "x" * 50000,
                "parameters": {"type": "object"},
            },
        }
    ]
    breakdown = RequestTokenBreakdown(
        bootstrap_prompt_tokens=10,
        tool_schema_tokens=5000,
        live_conversation_history_tokens=5000,
    )

    shape = _cache_prefix_compaction_shape(
        settings=settings,
        messages=messages,
        tool_list=tool_list,
        request_breakdown=breakdown,
        pinned_prefix_len=1,
        cache_policy={"enabled": True, "status": "enabled", "min_tokens": 4096},
    )

    assert shape.cacheable_surface_estimated_tokens >= 4096
    assert shape.cacheable_prefix_estimated_tokens < 4096
    assert shape.protected_prefix_message_count == 1
    assert "cacheable_prefix_below_min_tokens" in shape.reasons


def test_cache_prefix_shape_excludes_tool_schema_from_dynamic_suffix() -> None:
    settings = CompactionSettings(cache_aware_compaction=True)
    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
    ]
    tool_list = [
        {
            "type": "function",
            "function": {
                "name": "large_schema",
                "description": "x" * 50000,
                "parameters": {"type": "object"},
            },
        }
    ]
    schema_tokens = estimate_tool_schema_tokens(tool_list)
    breakdown = RequestTokenBreakdown(
        tool_schema_tokens=schema_tokens,
        live_conversation_history_tokens=estimate_message_tokens(messages),
    )

    shape = _cache_prefix_compaction_shape(
        settings=settings,
        messages=messages,
        tool_list=tool_list,
        request_breakdown=breakdown,
        pinned_prefix_len=1,
        cache_policy={"enabled": True, "status": "enabled", "min_tokens": 1},
    )
    decision = _cache_aware_compaction_decision(
        settings=settings,
        request_breakdown=breakdown,
        calibration={},
        prefix_shape=shape,
    )

    assert shape.dynamic_suffix_estimated_tokens < schema_tokens
    assert shape.dynamic_suffix_share < 0.25
    assert "large_dynamic_suffix" not in shape.reasons
    assert "large_dynamic_suffix" not in decision.reasons
    assert decision.reasons == ("large_tool_schema_share",)
