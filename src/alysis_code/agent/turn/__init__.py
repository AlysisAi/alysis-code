from __future__ import annotations

import importlib
from typing import Any

_LAZY_ATTR_MODULES = {
    "_event_preview": "events",
    "_emit_assistant_message_events": "events",
    "_emit_message_delta_event": "events",
    "_emit_message_end_event": "events",
    "_emit_tool_call_completed_event": "events",
    "_emit_tool_call_progress_event": "events",
    "_emit_tool_call_started_event": "events",
    "_ACTION_PROGRESS_FALLBACK_TOOL_NAMES": "exploration",
    "_ACTION_PROGRESS_TOOL_CATEGORIES": "exploration",
    "_EXPLORATION_FALLBACK_TOOL_NAMES": "exploration",
    "_EXPLORATION_TOOL_CATEGORIES": "exploration",
    "_FAILED_EDIT_STAGNATION_TOOL_NAMES": "exploration",
    "_UNEXECUTED_TOOL_CALL_MARKUP_MARKERS": "exploration",
    "MAX_RECENT_EXPLORATION_PATHS": "exploration",
    "_append_recent_exploration_path": "exploration",
    "_build_post_explore_bootstrap_nudge": "exploration",
    "_edit_similarity_key": "exploration",
    "_exploration_attempt_outcome": "exploration",
    "_exploration_similarity_key": "exploration",
    "_extract_successful_exploration_paths": "exploration",
    "_is_action_progress_tool": "exploration",
    "_is_exploration_only_tool": "exploration",
    "_is_failed_edit_stagnation_tool": "exploration",
    "_is_successful_subagent_run": "exploration",
    "_looks_like_unexecuted_tool_call_markup": "exploration",
    "_one_shot_progress_fingerprint": "exploration",
    "_tool_call_retry_key": "exploration",
    "_tool_categories": "exploration",
    "_SAME_BATCH_FS_READ_DEFAULT_MAX_BYTES": "read_cache",
    "_SAME_BATCH_FS_READ_LINES_DEFAULT_MAX_LINES": "read_cache",
    "_SAME_BATCH_READ_CACHE_SAFE_TOOL_NAMES": "read_cache",
    "_SameBatchFsReadLinesRecord": "read_cache",
    "_SameBatchFsReadRecord": "read_cache",
    "_SameBatchReadReuseCache": "read_cache",
    "_build_fs_read_lines_result_from_cached_range": "read_cache",
    "_build_fs_read_lines_result_from_full_fs_read": "read_cache",
    "_coerce_fs_read_lines_request": "read_cache",
    "_coerce_fs_read_request": "read_cache",
    "_maybe_reuse_same_batch_read_result": "read_cache",
    "_remember_same_batch_read_result": "read_cache",
    "_same_batch_read_cache_should_invalidate": "read_cache",
    "_same_batch_read_path_key": "read_cache",
    "_split_text_preserving_lines": "read_cache",
    "_SHELL_MUTATION_SNAPSHOT_METADATA_PREFIX": "snapshot",
    "_detect_command_mutation_paths": "snapshot",
    "_list_git_workspace_snapshot_paths": "snapshot",
    "_normalize_snapshot_ignore_paths": "snapshot",
    "_path_matches_snapshot_ignore": "snapshot",
    "_run_with_command_mutation_detection": "snapshot",
    "_snapshot_workspace_for_command_mutation_detection": "snapshot",
    "_walk_workspace_snapshot_paths": "snapshot",
    "_workspace_snapshot_signature": "snapshot",
}

_CORE_EXPORTS = {
    "_FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT",
    "_FORCED_FINAL_SUMMARY_SYSTEM_PROMPT_TEMPLATE",
    "_LOW_STEP_BUDGET_SYSTEM_PROMPT_TEMPLATE",
    "_PHASE_BUDGET_EXPLORATION_SYSTEM_PROMPT_TEMPLATE",
    "_PHASE_BUDGET_VERIFICATION_SYSTEM_PROMPT_TEMPLATE",
    "_SUBAGENT_REQUIRED_NUDGE_TEMPLATE",
    "MAX_EDIT_NUDGES_PER_TURN",
    "MAX_EXPLORATION_NUDGES_PER_TURN",
    "MAX_EXPLORATION_ONLY_STEPS_BEFORE_NUDGE",
    "MAX_FAILED_EDIT_STEPS_BEFORE_NUDGE",
    "MAX_IDENTICAL_EXPLORATION_ATTEMPTS",
    "MAX_IDENTICAL_FAILED_EDIT_ATTEMPTS",
    "MAX_IDENTICAL_TOOL_CALL_FAILURES",
    "MAX_NON_FINAL_CONTINUATIONS_PER_TURN",
    "MAX_POST_EXPLORE_BOOTSTRAP_NUDGES_PER_TURN",
    "MAX_SUBAGENT_REQUIRED_NUDGES_PER_TURN",
    "_SubagentTurnPolicy",
    "_SubagentTurnPolicyLevel",
    "_emit_surface_error",
    "_has_invalid_tool_call_json",
    "_resolve_subagent_turn_policy",
    "_subagent_names_preview",
    "_subagent_required_nudge_message",
    "_subagent_turn_context_message",
    "run_turn",
}

__all__ = tuple(sorted(_CORE_EXPORTS | set(_LAZY_ATTR_MODULES)))


def __getattr__(name: str) -> Any:
    module_name = _LAZY_ATTR_MODULES.get(name, "core")
    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
