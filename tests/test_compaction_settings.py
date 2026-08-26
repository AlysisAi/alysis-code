from __future__ import annotations

from alysis_code.compaction.settings import CompactionSettings, resolve_compaction_settings
from alysis_code.config import AppConfig


def test_compaction_settings_defaults() -> None:
    cfg = AppConfig(model="gpt-5-nano")
    settings = resolve_compaction_settings(cfg)
    assert settings == CompactionSettings()


def test_compaction_settings_reads_env(monkeypatch) -> None:
    cfg = AppConfig(model="gpt-5-nano")
    monkeypatch.setenv("ALYSIS_ENABLE_COMPACTION", "off")
    monkeypatch.setenv("ALYSIS_OFFLOAD_TOOL_OUTPUTS", "0")
    monkeypatch.setenv("ALYSIS_TOOL_OUTPUT_OFFLOAD_THRESHOLD_CHARS", "1234")
    monkeypatch.setenv("ALYSIS_TOOL_OUTPUT_PREVIEW_CHARS", "321")
    monkeypatch.setenv("ALYSIS_SUMMARIZE_CONVERSATION", "false")
    monkeypatch.setenv("ALYSIS_COMPACTION_RECENT_TURNS", "4")
    monkeypatch.setenv("ALYSIS_COMPACTION_TRIGGER_RATIO", "0.88")
    monkeypatch.setenv("ALYSIS_COMPACTION_TARGET_RATIO", "0.55")
    monkeypatch.setenv("ALYSIS_COMPACTION_MAX_CHUNK_MESSAGES", "77")
    monkeypatch.setenv("ALYSIS_COMPACTION_SAFETY_MARGIN_TOKENS", "700")
    monkeypatch.setenv("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_MESSAGES", "5")
    monkeypatch.setenv("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_TOKENS", "2048")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_ENABLED", "0")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_STRATEGY", "oldest")
    monkeypatch.setenv("ALYSIS_PIN_SCORE_THRESHOLD", "4.5")
    monkeypatch.setenv("ALYSIS_MAX_PINS", "11")
    monkeypatch.setenv("ALYSIS_MAX_PINS_CHARS", "2222")
    monkeypatch.setenv("ALYSIS_PIN_SNIPPET_CHARS", "333")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_USE_LLM", "true")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_LLM_MAX_TURNS", "9")
    monkeypatch.setenv("ALYSIS_CACHE_AWARE_COMPACTION", "false")
    monkeypatch.setenv("ALYSIS_CACHE_AWARE_MIN_TRIGGER_RATIO", "0.81")

    settings = resolve_compaction_settings(cfg)
    assert settings.enabled is False
    assert settings.offload_tool_outputs is False
    assert settings.tool_output_offload_threshold_chars == 1234
    assert settings.tool_output_preview_chars == 321
    assert settings.summarize_conversation is False
    assert settings.recent_user_turns_to_keep == 4
    assert settings.trigger_ratio == 0.88
    assert settings.target_ratio == 0.55
    assert settings.max_chunk_messages == 77
    assert settings.safety_margin_tokens == 700
    assert settings.execution_min_removable_messages == 5
    assert settings.execution_min_removable_tokens == 2048
    assert settings.importance_enabled is False
    assert settings.importance_strategy == "oldest"
    assert settings.pin_score_threshold == 4.5
    assert settings.max_pins == 11
    assert settings.max_pins_chars == 2222
    assert settings.pin_snippet_chars == 333
    assert settings.importance_use_llm is True
    assert settings.importance_llm_max_turns == 9
    assert settings.cache_aware_compaction is False
    assert settings.cache_aware_min_trigger_ratio == 0.81


def test_compaction_settings_cfg_overrides_env(monkeypatch) -> None:
    cfg = AppConfig(model="gpt-5-nano")
    cfg.extra_fields = {
        "compaction": {
            "enabled": True,
            "offload_tool_outputs": True,
            "tool_output_offload_threshold_chars": 9999,
            "tool_output_preview_chars": 1111,
            "summarize_conversation": False,
            "recent_user_turns_to_keep": 9,
            "trigger_ratio": 0.77,
            "target_ratio": 0.44,
            "max_chunk_messages": 66,
            "safety_margin_tokens": 333,
            "execution_min_removable_messages": 4,
            "execution_min_removable_tokens": 4096,
            "importance_enabled": True,
            "importance_strategy": "lowest_density",
            "pin_score_threshold": 8.5,
            "max_pins": 7,
            "max_pins_chars": 4444,
            "pin_snippet_chars": 111,
            "importance_use_llm": False,
            "importance_llm_max_turns": 15,
            "cache_aware_compaction": False,
            "cache_aware_min_trigger_ratio": 0.79,
        }
    }
    monkeypatch.setenv("ALYSIS_ENABLE_COMPACTION", "off")
    monkeypatch.setenv("ALYSIS_OFFLOAD_TOOL_OUTPUTS", "0")
    monkeypatch.setenv("ALYSIS_TOOL_OUTPUT_OFFLOAD_THRESHOLD_CHARS", "1234")
    monkeypatch.setenv("ALYSIS_TOOL_OUTPUT_PREVIEW_CHARS", "321")
    monkeypatch.setenv("ALYSIS_SUMMARIZE_CONVERSATION", "true")
    monkeypatch.setenv("ALYSIS_COMPACTION_RECENT_TURNS", "2")
    monkeypatch.setenv("ALYSIS_COMPACTION_TRIGGER_RATIO", "0.9")
    monkeypatch.setenv("ALYSIS_COMPACTION_TARGET_RATIO", "0.7")
    monkeypatch.setenv("ALYSIS_COMPACTION_MAX_CHUNK_MESSAGES", "5")
    monkeypatch.setenv("ALYSIS_COMPACTION_SAFETY_MARGIN_TOKENS", "1000")
    monkeypatch.setenv("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_MESSAGES", "2")
    monkeypatch.setenv("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_TOKENS", "1500")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_ENABLED", "0")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_STRATEGY", "oldest")
    monkeypatch.setenv("ALYSIS_PIN_SCORE_THRESHOLD", "1.0")
    monkeypatch.setenv("ALYSIS_MAX_PINS", "30")
    monkeypatch.setenv("ALYSIS_MAX_PINS_CHARS", "9000")
    monkeypatch.setenv("ALYSIS_PIN_SNIPPET_CHARS", "700")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_USE_LLM", "true")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_LLM_MAX_TURNS", "2")
    monkeypatch.setenv("ALYSIS_CACHE_AWARE_COMPACTION", "true")
    monkeypatch.setenv("ALYSIS_CACHE_AWARE_MIN_TRIGGER_RATIO", "0.82")

    settings = resolve_compaction_settings(cfg)
    assert settings.enabled is True
    assert settings.offload_tool_outputs is True
    assert settings.tool_output_offload_threshold_chars == 9999
    assert settings.tool_output_preview_chars == 1111
    assert settings.summarize_conversation is False
    assert settings.recent_user_turns_to_keep == 9
    assert settings.trigger_ratio == 0.77
    assert settings.target_ratio == 0.44
    assert settings.max_chunk_messages == 66
    assert settings.safety_margin_tokens == 333
    assert settings.execution_min_removable_messages == 4
    assert settings.execution_min_removable_tokens == 4096
    assert settings.importance_enabled is True
    assert settings.importance_strategy == "lowest_density"
    assert settings.pin_score_threshold == 8.5
    assert settings.max_pins == 7
    assert settings.max_pins_chars == 4444
    assert settings.pin_snippet_chars == 111
    assert settings.importance_use_llm is False
    assert settings.importance_llm_max_turns == 15
    assert settings.cache_aware_compaction is False
    assert settings.cache_aware_min_trigger_ratio == 0.79


def test_compaction_settings_invalid_values_fall_back_to_defaults(monkeypatch) -> None:
    cfg = AppConfig(model="gpt-5-nano")
    cfg.extra_fields = {
        "compaction": {
            "enabled": "not-bool",
            "offload_tool_outputs": "nah",
            "tool_output_offload_threshold_chars": -1,
            "tool_output_preview_chars": "oops",
            "summarize_conversation": "bad",
            "recent_user_turns_to_keep": -1,
            "trigger_ratio": 1.2,
            "target_ratio": -0.1,
            "max_chunk_messages": "x",
            "safety_margin_tokens": 0,
            "execution_min_removable_messages": -1,
            "execution_min_removable_tokens": "oops",
            "importance_enabled": "bad",
            "importance_strategy": "weird",
            "pin_score_threshold": "nan",
            "max_pins": 0,
            "max_pins_chars": -1,
            "pin_snippet_chars": "bad",
            "importance_use_llm": "???",
            "importance_llm_max_turns": 0,
            "cache_aware_compaction": "bad",
            "cache_aware_min_trigger_ratio": 1.0,
        }
    }
    monkeypatch.setenv("ALYSIS_ENABLE_COMPACTION", "invalid")
    monkeypatch.setenv("ALYSIS_OFFLOAD_TOOL_OUTPUTS", "invalid")
    monkeypatch.setenv("ALYSIS_TOOL_OUTPUT_OFFLOAD_THRESHOLD_CHARS", "0")
    monkeypatch.setenv("ALYSIS_TOOL_OUTPUT_PREVIEW_CHARS", "-10")
    monkeypatch.setenv("ALYSIS_SUMMARIZE_CONVERSATION", "invalid")
    monkeypatch.setenv("ALYSIS_COMPACTION_RECENT_TURNS", "0")
    monkeypatch.setenv("ALYSIS_COMPACTION_TRIGGER_RATIO", "0.4")
    monkeypatch.setenv("ALYSIS_COMPACTION_TARGET_RATIO", "0.6")
    monkeypatch.setenv("ALYSIS_COMPACTION_MAX_CHUNK_MESSAGES", "-5")
    monkeypatch.setenv("ALYSIS_COMPACTION_SAFETY_MARGIN_TOKENS", "0")
    monkeypatch.setenv("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_MESSAGES", "0")
    monkeypatch.setenv("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_TOKENS", "-10")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_ENABLED", "invalid")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_STRATEGY", "not-a-strategy")
    monkeypatch.setenv("ALYSIS_PIN_SCORE_THRESHOLD", "-1")
    monkeypatch.setenv("ALYSIS_MAX_PINS", "0")
    monkeypatch.setenv("ALYSIS_MAX_PINS_CHARS", "0")
    monkeypatch.setenv("ALYSIS_PIN_SNIPPET_CHARS", "-4")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_USE_LLM", "invalid")
    monkeypatch.setenv("ALYSIS_IMPORTANCE_LLM_MAX_TURNS", "-2")
    monkeypatch.setenv("ALYSIS_CACHE_AWARE_COMPACTION", "invalid")
    monkeypatch.setenv("ALYSIS_CACHE_AWARE_MIN_TRIGGER_RATIO", "0.99")

    settings = resolve_compaction_settings(cfg)
    assert settings == CompactionSettings()
