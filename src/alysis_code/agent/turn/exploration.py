from __future__ import annotations

import json
import re
import shlex
from collections.abc import Collection
from pathlib import Path
from typing import Any

from ...text_normalization import normalize_for_matching
from ...tools.registry import get_builtin_tool_metadata
from ...verification_command_analysis import (
    VerificationCommandEvidentiaryCapability,
    analyze_verification_command,
)
from ..prompt_context import (
    MAX_POST_EXPLORE_ANCHOR_PATHS,
    _extract_repo_relative_paths_from_text,
    _normalize_repo_relative_hint_path,
)
from ..verification import _runtime_message

MAX_RECENT_EXPLORATION_PATHS = 12
_EXPLORATION_FALLBACK_TOOL_NAMES = {
    "fs_read",
    "fs_read_lines",
    "fs_list",
    "symbol_search",
    "repo_map",
    "search_rg",
    "history_search",
    "git_status",
    "git_diff",
    "git_history",
}
_ACTION_PROGRESS_FALLBACK_TOOL_NAMES = {
    "fs_edit",
    "fs_move",
    "fs_copy",
    "fs_delete",
    "fs_mkdir",
    "fs_write",
    "git_apply_patch",
    "verify_run",
}
_ACTION_PROGRESS_TOOL_CATEGORIES = {"write", "verify", "subagent"}
_EXPLORATION_TOOL_CATEGORIES = {"read", "search", "history"}
_SUBAGENT_READ_CONTROL_TOOL_NAMES = {
    "subagent_cancel",
    "subagent_send",
    "subagent_status",
}
_SUBAGENT_EFFECT_CLASSIFIED_TOOL_NAMES = {
    "subagent_run",
    "subagent_spawn",
    "subagent_wait",
}
_FAILED_EDIT_STAGNATION_TOOL_NAMES = {"fs_edit", "git_apply_patch", "fs_write"}
_SHELL_TOOL_NAMES = {
    "shell_run",
    "shell_background",
    "shell_service_start",
    "shell_service_status",
    "workspace_preview_start",
}
_SHELL_SERVICE_PROGRESS_TOOL_NAMES = {
    "shell_background",
    "shell_service_start",
    "workspace_preview_start",
}
_UNEXECUTED_TOOL_CALL_MARKUP_MARKERS = (
    "<tool_call",
    "</tool_call",
    "tool_calls",
    "<function_calls",
    "</function_calls",
)


def _exploration_attempt_outcome(success_count: int, failed_count: int) -> str:
    if success_count > 0 and failed_count > 0:
        return "mixed"
    if failed_count > 0:
        return "failed"
    if success_count > 0:
        return "successful"
    return "none"


def _one_shot_progress_fingerprint(text: str) -> str:
    normalized = normalize_for_matching(text)
    return re.sub(r"[^\w ]+", "", normalized, flags=re.UNICODE)


def _stagnation_detection_event_should_emit(detection_count: int, *, nudge_sent: bool) -> bool:
    """Whether the Nth stagnation detection of an episode should be recorded.

    Once stagnation locks in, the detector fires on every subsequent step, so a
    long stagnating turn used to log one near-identical event per step (50+ per
    session). Detections that actually send a nudge are always recorded;
    otherwise emission is spaced at power-of-two counts (1, 2, 4, 8, ...), so a
    stagnating turn logs O(log n) events with unchanged prompt behavior — the
    suppressed occurrences are summarized on the next emitted event.
    """

    if nudge_sent:
        return True
    if detection_count <= 0:
        return False
    return detection_count & (detection_count - 1) == 0


def _tool_call_retry_key(name: str, arguments: dict[str, Any]) -> str:
    try:
        payload = json.dumps(arguments, ensure_ascii=True, sort_keys=True)
    except TypeError:
        payload = json.dumps(str(arguments), ensure_ascii=True)
    return f"{name}:{payload}"


def _exploration_similarity_key(name: str, arguments: dict[str, Any]) -> str:
    relevant_fields = (
        "path",
        "root_path",
        "cmd",
        "query",
        "pattern",
        "ref",
        "commit",
    )
    parts = [name]
    for key_name in relevant_fields:
        value = arguments.get(key_name)
        if value is None:
            continue
        parts.append(f"{key_name}={value}")
    return "|".join(parts)


def _edit_similarity_key(name: str, arguments: dict[str, Any]) -> str:
    path = str(arguments.get("path") or "").strip()
    source_path = str(arguments.get("source_path") or "").strip()
    destination_path = str(arguments.get("destination_path") or "").strip()
    ops = arguments.get("edits")
    op_names: list[str] = []
    if isinstance(ops, list):
        for raw in ops:
            if isinstance(raw, dict):
                op = raw.get("op")
                if isinstance(op, str):
                    op_names.append(op.strip())
    op_signature = ",".join(op_names[:5])
    return "|".join(
        [
            name,
            f"path={path}",
            f"source={source_path}",
            f"destination={destination_path}",
            f"ops={op_signature}",
        ]
    )


def _append_recent_exploration_path(
    *,
    paths: list[str],
    candidate: str | None,
    max_items: int = MAX_RECENT_EXPLORATION_PATHS,
) -> None:
    if not candidate:
        return
    normalized = candidate.strip()
    if not normalized or normalized == ".":
        return
    existing_idx = None
    for idx, item in enumerate(paths):
        if item.casefold() == normalized.casefold():
            existing_idx = idx
            break
    if existing_idx is not None:
        paths.pop(existing_idx)
    paths.append(normalized)
    if len(paths) > max_items:
        del paths[0 : len(paths) - max_items]


def _looks_like_unexecuted_tool_call_markup(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    if "dsml" in normalized and ("tool_calls" in normalized or "invoke" in normalized):
        return True
    return any(marker in normalized for marker in _UNEXECUTED_TOOL_CALL_MARKUP_MARKERS)


def _extract_successful_exploration_paths(
    *,
    root: Path,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    max_items: int = MAX_POST_EXPLORE_ANCHOR_PATHS,
) -> list[str]:
    normalized_tool = tool_name.strip().lower()
    out: list[str] = []

    for key in ("path", "root_path"):
        value = arguments.get(key)
        if isinstance(value, str):
            normalized = _normalize_repo_relative_hint_path(root=root, raw=value)
            if normalized and not any(
                existing.casefold() == normalized.casefold() for existing in out
            ):
                out.append(normalized)
                if len(out) >= max_items:
                    return out

    if normalized_tool == "fs_list":
        result_root = _normalize_repo_relative_hint_path(
            root=root, raw=str(result.get("root") or "")
        )
        entries = result.get("entries")
        if isinstance(entries, list):
            for entry in entries[: max_items * 3]:
                if not isinstance(entry, dict):
                    continue
                rel = str(entry.get("path") or "")
                if not rel:
                    continue
                if result_root and result_root != ".":
                    combined = f"{result_root.rstrip('/')}/{rel.lstrip('./')}"
                else:
                    combined = rel
                normalized = _normalize_repo_relative_hint_path(root=root, raw=combined)
                if normalized and not any(
                    existing.casefold() == normalized.casefold() for existing in out
                ):
                    out.append(normalized)
                    if len(out) >= max_items:
                        return out

    if normalized_tool == "search_rg":
        matches = result.get("matches")
        if isinstance(matches, list):
            for match in matches[: max_items * 3]:
                if not isinstance(match, dict):
                    continue
                normalized = _normalize_repo_relative_hint_path(
                    root=root,
                    raw=str(match.get("path") or ""),
                )
                if normalized and not any(
                    existing.casefold() == normalized.casefold() for existing in out
                ):
                    out.append(normalized)
                    if len(out) >= max_items:
                        return out

    if normalized_tool == "subagent_run":
        subagent_result = str(result.get("result") or "")
        for candidate in _extract_repo_relative_paths_from_text(
            root=root,
            text=subagent_result,
            max_items=max_items,
        ):
            if any(existing.casefold() == candidate.casefold() for existing in out):
                continue
            out.append(candidate)
            if len(out) >= max_items:
                break

    return out


def _is_successful_subagent_run(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
) -> bool:
    if tool_name.strip().lower() != "subagent_run":
        return False
    if status == "failed":
        return False
    return bool(str(result.get("subagent") or arguments.get("name") or "").strip())


def _build_post_explore_bootstrap_nudge(
    *,
    anchor_paths: list[str],
    language: str = "",
    explicit_language_override: bool = False,
) -> str:
    message = _runtime_message(
        "one_shot_post_explore_bootstrap_nudge",
        language=language,
        explicit_language_override=explicit_language_override,
    )
    if anchor_paths:
        joined = ", ".join(anchor_paths[:MAX_POST_EXPLORE_ANCHOR_PATHS])
        message += " " + _runtime_message(
            "one_shot_post_explore_bootstrap_targets",
            language=language,
            explicit_language_override=explicit_language_override,
            joined=joined,
        )
    return message


def _tool_categories(tool_name: str) -> set[str]:
    metadata = get_builtin_tool_metadata(tool_name)
    if metadata is None:
        return set()
    return {str(category).strip().lower() for category in metadata.categories}


def _normal_tool_name(tool_name: str) -> str:
    return str(tool_name or "").strip().lower()


def _shell_cmd(arguments: dict[str, Any] | None) -> str:
    if not isinstance(arguments, dict):
        return ""
    return str(arguments.get("cmd") or arguments.get("command") or "").strip()


def _shell_exit_code(result: dict[str, Any] | None) -> int | None:
    if not isinstance(result, dict):
        return None
    value = result.get("exit_code")
    return value if isinstance(value, int) else None


def _shell_touched_paths(
    *,
    result: dict[str, Any] | None,
    touched_paths: Collection[str] | None,
) -> tuple[str, ...]:
    out: list[str] = []
    for raw in touched_paths or ():
        text = str(raw or "").strip()
        if text:
            out.append(text)
    if isinstance(result, dict):
        for key in (
            "touched_repo_paths",
            "material_touched_repo_paths",
            "verification_relevant_touched_paths",
        ):
            value = result.get(key)
            if isinstance(value, (list, tuple, set)):
                for raw in value:
                    text = str(raw or "").strip()
                    if text:
                        out.append(text)
    deduped: list[str] = []
    for item in out:
        if not any(existing.casefold() == item.casefold() for existing in deduped):
            deduped.append(item)
    return tuple(deduped)


def _shell_result_has_mutation_effect(
    *,
    result: dict[str, Any] | None,
    touched_paths: Collection[str] | None,
) -> bool:
    return bool(_shell_touched_paths(result=result, touched_paths=touched_paths))


def _shell_command_is_assertive_verification(
    *,
    cmd: str,
    result: dict[str, Any] | None,
) -> bool:
    if not cmd:
        return False
    if isinstance(result, dict) and (
        bool(result.get("trusted_shell_verification"))
        or bool(result.get("verification_evidence_allowed"))
    ):
        return True
    try:
        analysis = analyze_verification_command(cmd, trusted=False)
    except Exception:  # noqa: BLE001
        return False
    if analysis.rejection_reason:
        return False
    return analysis.evidentiary_capability == VerificationCommandEvidentiaryCapability.ASSERTIVE


def _shell_command_head(cmd: str) -> str:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return ""
    if not parts:
        return ""
    head = parts[0].rsplit("/", 1)[-1].casefold()
    if (
        head in {"python", "python3", "node"}
        and len(parts) >= 3
        and parts[1]
        in {
            "-m",
            "-c",
        }
    ):
        return f"{head} {parts[1]} {parts[2].casefold()}"
    return head


def _shell_command_is_low_value_exploration(cmd: str) -> bool:
    head = _shell_command_head(cmd)
    if not head:
        return True
    if head in {
        "awk",
        "cat",
        "cut",
        "env",
        "file",
        "find",
        "grep",
        "head",
        "ls",
        "pwd",
        "rg",
        "sed",
        "stat",
        "tail",
        "type",
        "wc",
        "which",
    }:
        return True
    return head.startswith(("python -c", "python3 -c", "node -c"))


def _shell_command_is_focused_progress(
    *,
    cmd: str,
    focused: bool,
    result: dict[str, Any] | None,
) -> bool:
    if not focused or not cmd:
        return False
    if _shell_command_is_low_value_exploration(cmd):
        return False
    exit_code = _shell_exit_code(result)
    return exit_code is None or exit_code == 0


def _is_shell_action_progress_tool(
    tool_name: str,
    *,
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    touched_paths: Collection[str] | None = None,
    focused: bool = False,
) -> bool:
    normalized = _normal_tool_name(tool_name)
    if normalized in _SHELL_SERVICE_PROGRESS_TOOL_NAMES:
        return True
    if normalized == "shell_service_status":
        return isinstance(result, dict) and bool(result.get("ready") or result.get("healthy"))
    if normalized != "shell_run":
        return False
    if _shell_result_has_mutation_effect(result=result, touched_paths=touched_paths):
        return True
    cmd = _shell_cmd(arguments)
    if _shell_command_is_assertive_verification(cmd=cmd, result=result):
        return True
    return _shell_command_is_focused_progress(cmd=cmd, focused=focused, result=result)


def _subagent_result_has_action_effect(
    *,
    result: dict[str, Any] | None,
    touched_paths: Collection[str] | None,
) -> bool:
    if touched_paths:
        return True
    if not isinstance(result, dict):
        return False
    candidates = [result]
    nested_results = result.get("results")
    if isinstance(nested_results, dict):
        candidates.extend(
            candidate for candidate in nested_results.values() if isinstance(candidate, dict)
        )
    for candidate in candidates:
        raw_effects = candidate.get("effects")
        effect_names = (
            {str(effect or "").strip().lower() for effect in raw_effects if str(effect or "")}
            if isinstance(raw_effects, (list, tuple, set))
            else set()
        )
        if effect_names & {"write_workspace", "run_commands", "external_write"}:
            return True
        if _shell_touched_paths(result=candidate, touched_paths=None):
            return True
    return False


def _is_subagent_action_progress_tool(
    tool_name: str,
    *,
    arguments: dict[str, Any] | None,
    result: dict[str, Any] | None,
    touched_paths: Collection[str] | None,
) -> bool:
    normalized = _normal_tool_name(tool_name)
    if normalized in _SUBAGENT_READ_CONTROL_TOOL_NAMES:
        return False
    if normalized == "subagent_spawn":
        workspace_view = str((arguments or {}).get("workspace_view") or "shared")
        return workspace_view.strip().lower() == "isolated"
    return _subagent_result_has_action_effect(
        result=result,
        touched_paths=touched_paths,
    )


def _is_action_progress_tool(
    tool_name: str,
    *,
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    touched_paths: Collection[str] | None = None,
    focused: bool = False,
) -> bool:
    normalized = _normal_tool_name(tool_name)
    if normalized in (_SUBAGENT_READ_CONTROL_TOOL_NAMES | _SUBAGENT_EFFECT_CLASSIFIED_TOOL_NAMES):
        return _is_subagent_action_progress_tool(
            normalized,
            arguments=arguments,
            result=result,
            touched_paths=touched_paths,
        )
    if normalized in _SHELL_TOOL_NAMES:
        return _is_shell_action_progress_tool(
            normalized,
            arguments=arguments,
            result=result,
            touched_paths=touched_paths,
            focused=focused,
        )
    categories = _tool_categories(tool_name)
    if categories:
        return bool(categories & _ACTION_PROGRESS_TOOL_CATEGORIES)
    return normalized in _ACTION_PROGRESS_FALLBACK_TOOL_NAMES


def _is_exploration_only_tool(
    tool_name: str,
    *,
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    touched_paths: Collection[str] | None = None,
    focused: bool = False,
) -> bool:
    normalized = _normal_tool_name(tool_name)
    if normalized in (_SUBAGENT_READ_CONTROL_TOOL_NAMES | _SUBAGENT_EFFECT_CLASSIFIED_TOOL_NAMES):
        return not _is_action_progress_tool(
            normalized,
            arguments=arguments,
            result=result,
            touched_paths=touched_paths,
            focused=focused,
        )
    if normalized in _SHELL_TOOL_NAMES:
        return not _is_shell_action_progress_tool(
            normalized,
            arguments=arguments,
            result=result,
            touched_paths=touched_paths,
            focused=focused,
        )
    categories = _tool_categories(tool_name)
    if categories:
        if categories & _ACTION_PROGRESS_TOOL_CATEGORIES:
            return False
        return bool(categories & _EXPLORATION_TOOL_CATEGORIES)
    return normalized in _EXPLORATION_FALLBACK_TOOL_NAMES


def _is_failed_edit_stagnation_tool(tool_name: str) -> bool:
    return tool_name.strip().lower() in _FAILED_EDIT_STAGNATION_TOOL_NAMES
