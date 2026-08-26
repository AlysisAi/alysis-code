"""IDE bridge support for Forge swarm jobs: events, result payloads, reconcile.

Kept separate from ``forge_protocol.py`` on purpose: that module is pinned to
never reference swarm or subprocess machinery. Everything here is either a
pure payload builder, a trace-to-protocol event adapter, or an explicitly
invoked reconcile action over run artifacts.

Reconcile is READ-ONLY by default: it enumerates plan state, run artifacts,
and preserved task worktrees without touching the backend ensure/reuse
lifecycle. Mutation happens only through the explicit ``harvest`` (write
per-task diff artifacts under the run) and ``discard`` (drop preserved
worktrees) actions, both idempotent.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from ..forge import RunPaths, find_task, now_iso, save_plan, set_task_status
from ..git_ops import (
    GitOpsError,
    _run_git_checked,
    branch_exists,
    current_branch,
    untracked_files,
)
from ..git_worktrees import prune_worktrees, remove_task_worktree
from ..swarm_scheduler import canonical_task_status
from ..swarm_trace import SwarmTraceEvent
from .forge_protocol import ForgePlanRecord, forge_artifact_root_name
from .protocol import ProtocolError, redact_secrets

SWARM_EVENT_MESSAGE_MAX_CHARS = 300
SWARM_TASK_EVENT_STATES = (
    "scheduled",
    "started",
    "progress",
    "approval_pending",  # reserved placeholder; emitted once approval routing lands
    "interrupted",
    "failed",
    "merged",
)
RECONCILE_ACTIONS = ("report", "harvest", "discard")
_HARVEST_DIR_NAME = "harvest"


def working_tree_diff_against(root: Path, *, base: str) -> str:
    """Return committed and tracked working-tree changes against ``base``.

    This IDE-only compatibility helper uses the current hardened git runner;
    untracked files remain a separate, explicit part of the review payload.
    """
    completed = _run_git_checked(
        root,
        ["diff", "--binary", "-M", base, "--"],
        error_message="failed to generate working tree diff",
        timeout_s=5.0,
    )
    return completed.stdout


def apply_patch_file(root: Path, *, patch_path: Path) -> None:
    """Apply one reviewed patch after a fail-closed ``git apply --check``."""
    _run_git_checked(
        root,
        ["apply", "--check", "--whitespace=nowarn", str(patch_path)],
        error_message="patch does not apply cleanly to the working tree",
    )
    _run_git_checked(
        root,
        ["apply", "--whitespace=nowarn", str(patch_path)],
        error_message="failed to apply patch to the working tree",
    )


def _remove_readonly_for_rmtree(function: Any, path: str, _exc_info: Any) -> None:
    """Retry removal after clearing Windows' read-only file attribute."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _bounded_redacted(message: str) -> str:
    clean = str(redact_secrets(str(message or "")))
    if len(clean) > SWARM_EVENT_MESSAGE_MAX_CHARS:
        return clean[: SWARM_EVENT_MESSAGE_MAX_CHARS - 3] + "..."
    return clean


class BridgeSwarmTraceSink:
    """Map engine swarm-trace events onto typed protocol surface events.

    Task-scoped lifecycle lands on the already-registered
    ``swarm_worker_state_changed`` envelope (worker_id = task id, state from
    ``SWARM_TASK_EVENT_STATES``); warnings land on ``warning_emitted``. The
    surface records every event into the session's bounded replay ring, so
    reconnecting IDE clients replay swarm progress. Run-level started/
    completed/cancelled events are emitted by the bridge job runner itself.

    Thread-safety: ``emit`` is called from worker threads and the
    orchestrator thread; the underlying surface serializes writes.
    """

    def __init__(self, surface: Any) -> None:
        self._surface = surface

    def close(self) -> None:
        return None

    def emit(self, event: SwarmTraceEvent) -> None:
        task_id = str(event.task_id or "").strip()
        phase = str(event.phase or "")
        message = _bounded_redacted(event.message)
        state: str | None = None
        if task_id:
            if phase == "worktree.lifecycle" and message.startswith("Preparing worktree"):
                state = "scheduled"
            elif phase == "worker.lifecycle" and message.startswith("Worker started"):
                state = "started"
            elif phase == "worker.lifecycle" and message.startswith("Worker interrupted"):
                state = "interrupted"
            elif phase == "worker.lifecycle" and message.startswith("Worker finished"):
                state = "progress"
            elif phase == "worker.error":
                state = "failed"
            elif phase == "merge.lifecycle" and (
                "merged" in message or "applied" in message or "already-satisfied" in message
            ):
                state = "merged"
            elif phase in {"scope.violation", "worker.warning"}:
                self._surface.emit_warning(f"[{task_id}] {message}")
                return
        elif phase == "worker.warning":
            self._surface.emit_warning(message)
            return
        if state is not None:
            self._surface.emit_swarm_worker_state_changed(task_id, state, role="forge_swarm")


def swarm_run_summary_payload(paths: RunPaths) -> dict[str, Any]:
    summary_path = paths.execution_dir / "swarm_summary.json"
    if not summary_path.exists():
        return {}
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def forge_swarm_result_payload(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    exit_code: int,
) -> dict[str, Any]:
    summary = swarm_run_summary_payload(record.paths)
    counts: dict[str, int] = {}
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        counts[status] = counts.get(status, 0) + 1
    summary_md = record.paths.execution_dir / "swarm_summary.md"
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "exit_code": exit_code,
        "run_status": str(summary.get("status") or ("clean" if exit_code == 0 else "unknown")),
        "clean": bool(summary.get("clean", exit_code == 0)),
        "interrupted": bool(summary.get("interrupted", False)),
        "interrupted_task_ids": [str(item) for item in (summary.get("interrupted_task_ids") or [])],
        "verification_status": str(summary.get("verification_status") or "unknown"),
        "reason_codes": [str(item) for item in (summary.get("reason_codes") or [])],
        "task_status_counts": counts,
        "summary_artifact_present": summary_md.exists(),
        "redacted": True,
        "secret_values_included": False,
    }


def _task_worktree_path(paths: RunPaths, task_id: str) -> Path:
    return paths.run_dir / "worktrees" / task_id / "repo"


def _merge_result_payload(paths: RunPaths, task_id: str) -> dict[str, Any] | None:
    merge_path = paths.execution_dir / "merge_results" / f"{task_id}.json"
    if not merge_path.exists():
        return None
    try:
        payload = json.loads(merge_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _harvest_dir(paths: RunPaths) -> Path:
    return paths.execution_dir / _HARVEST_DIR_NAME


def _classify_task(
    paths: RunPaths,
    task: dict[str, Any],
) -> dict[str, Any]:
    task_id = str(task.get("id") or "").strip()
    status = canonical_task_status(str(task.get("status") or ""))
    worktree = _task_worktree_path(paths, task_id)
    worktree_present = worktree.is_dir()
    merge_payload = _merge_result_payload(paths, task_id)
    merged = status == "done" or bool(merge_payload and merge_payload.get("success"))
    patch_artifact = paths.execution_patches_dir / f"{task_id}.diff"
    harvest_artifact = _harvest_dir(paths) / f"{task_id}.diff"
    if merged:
        state = "merged"
    elif status == "interrupted":
        state = "interrupted"
    elif status in {"planned", "todo"} and not worktree_present:
        state = "unstarted"
    else:
        state = status
    diff_available = worktree_present or patch_artifact.exists() or harvest_artifact.exists()
    return {
        "task_id": task_id,
        "status": status,
        "state": state,
        "worktree_present": worktree_present,
        "worktree_path": (
            os.fspath(worktree.relative_to(paths.root))
            if worktree_present and worktree.is_relative_to(paths.root)
            else (os.fspath(worktree) if worktree_present else None)
        ),
        "diff_available": diff_available,
        "patch_artifact_present": patch_artifact.exists(),
        "harvest_artifact_present": harvest_artifact.exists(),
        "merge_commit_hash": (
            str(merge_payload.get("merge_commit_hash"))
            if merge_payload and merge_payload.get("merge_commit_hash")
            else None
        ),
        "branch": str(task.get("branch") or "") or None,
    }


def _resolve_reconcile_base_branch(paths: RunPaths, base_branch: str | None) -> str:
    if base_branch:
        return base_branch
    try:
        return current_branch(paths.root)
    except GitOpsError:
        return "main"


def _resolve_worktree_base_branch(
    paths: RunPaths,
    worktree: Path,
    base_branch: str | None,
) -> str:
    """Pick the diff base that actually exists in the task worktree.

    Snapshot-backend worktrees carry a synthetic ``snapshot-base`` branch;
    git-worktree backends share branches with the control repo.
    """
    candidates: list[str] = []
    if base_branch:
        candidates.append(base_branch)
    try:
        candidates.append(current_branch(paths.root))
    except GitOpsError:
        pass
    candidates.extend(["snapshot-base", "main"])
    for candidate in candidates:
        clean = str(candidate or "").strip()
        if not clean:
            continue
        try:
            if branch_exists(worktree, clean):
                return clean
        except GitOpsError:
            continue
    return base_branch or "main"


def _harvest_task(
    paths: RunPaths,
    entry: dict[str, Any],
    *,
    base_branch: str,
) -> dict[str, Any]:
    task_id = str(entry["task_id"])
    worktree = _task_worktree_path(paths, task_id)
    harvest_dir = _harvest_dir(paths)
    harvest_dir.mkdir(parents=True, exist_ok=True)
    diff_path = harvest_dir / f"{task_id}.diff"
    meta_path = harvest_dir / f"{task_id}.json"
    if not worktree.is_dir():
        return {
            "task_id": task_id,
            "harvested": False,
            "reason": "worktree_absent",
            "diff_artifact": (
                os.fspath(diff_path.relative_to(paths.root)) if diff_path.exists() else None
            ),
        }
    resolved_base = _resolve_worktree_base_branch(paths, worktree, base_branch)
    try:
        diff_text = working_tree_diff_against(worktree, base=resolved_base)
        untracked = untracked_files(worktree)
    except GitOpsError as exc:
        return {
            "task_id": task_id,
            "harvested": False,
            "reason": f"git_error: {redact_secrets(str(exc))}",
            "diff_artifact": None,
        }
    diff_path.write_text(
        diff_text if diff_text else "(empty diff against base)\n", encoding="utf-8"
    )
    meta_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "base_branch": resolved_base,
                "diff_bytes": len(diff_text.encode("utf-8", errors="replace")),
                "untracked_files": sorted(untracked),
                "source_worktree": os.fspath(worktree),
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "task_id": task_id,
        "harvested": True,
        "diff_artifact": os.fspath(diff_path.relative_to(paths.root)),
        "diff_bytes": len(diff_text.encode("utf-8", errors="replace")),
        "untracked_file_count": len(untracked),
    }


def _discard_task_worktree(paths: RunPaths, entry: dict[str, Any]) -> dict[str, Any]:
    task_id = str(entry["task_id"])
    worktree = _task_worktree_path(paths, task_id)
    if not worktree.is_dir():
        return {"task_id": task_id, "discarded": False, "reason": "already_absent"}
    try:
        remove_task_worktree(root=paths.root, worktree_repo_path=worktree, force=True)
    except GitOpsError:
        # Snapshot-backend workspaces and synthetic dirs are not registered
        # git worktrees; fall back to a contained recursive delete strictly
        # under the run's worktrees directory.
        resolved = worktree.resolve()
        worktrees_root = (paths.run_dir / "worktrees").resolve()
        if not resolved.is_relative_to(worktrees_root):
            return {
                "task_id": task_id,
                "discarded": False,
                "reason": "refused_outside_run_worktrees_dir",
            }
        try:
            shutil.rmtree(resolved, onerror=_remove_readonly_for_rmtree)
        except OSError as exc:
            return {
                "task_id": task_id,
                "discarded": False,
                "reason": "worktree_delete_failed",
                "error": str(redact_secrets(str(exc))),
            }
        if resolved.exists():
            return {
                "task_id": task_id,
                "discarded": False,
                "reason": "worktree_delete_incomplete",
            }
    try:
        prune_worktrees(paths.root)
    except GitOpsError:
        pass
    parent = worktree.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    return {"task_id": task_id, "discarded": True}


def forge_swarm_reconcile_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    action: str = "report",
    task_ids: list[str] | None = None,
    base_branch: str | None = None,
) -> dict[str, Any]:
    clean_action = str(action or "report").strip().lower()
    if clean_action not in RECONCILE_ACTIONS:
        raise ProtocolError(
            "invalid_reconcile_action",
            "forge.swarm.reconcile action must be report, harvest, or discard.",
        )
    paths = record.paths
    wanted = {str(item).strip() for item in (task_ids or []) if str(item).strip()} or None
    if wanted is not None:
        for task_id in sorted(wanted):
            if find_task(plan, task_id) is None:
                raise ProtocolError("task_not_found", f"Unknown task id for reconcile: {task_id}")
    entries = [
        _classify_task(paths, task)
        for task in (plan.get("tasks") or [])
        if isinstance(task, dict)
        and str(task.get("id") or "").strip()
        and (wanted is None or str(task.get("id") or "").strip() in wanted)
    ]
    actions: list[dict[str, Any]] = []
    if clean_action == "harvest":
        resolved_base = _resolve_reconcile_base_branch(paths, base_branch)
        actions = [
            _harvest_task(paths, entry, base_branch=resolved_base)
            for entry in entries
            if entry["worktree_present"]
        ]
        # Refresh classification so harvest artifacts show in the report.
        entries = [
            _classify_task(paths, task)
            for task in (plan.get("tasks") or [])
            if isinstance(task, dict)
            and str(task.get("id") or "").strip()
            and (wanted is None or str(task.get("id") or "").strip() in wanted)
        ]
    elif clean_action == "discard":
        actions = [
            _discard_task_worktree(paths, entry) for entry in entries if entry["worktree_present"]
        ]
        entries = [
            _classify_task(paths, task)
            for task in (plan.get("tasks") or [])
            if isinstance(task, dict)
            and str(task.get("id") or "").strip()
            and (wanted is None or str(task.get("id") or "").strip() in wanted)
        ]
    counts: dict[str, int] = {}
    for entry in entries:
        counts[str(entry["state"])] = counts.get(str(entry["state"]), 0) + 1
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "action": clean_action,
        "tasks": entries,
        "state_counts": counts,
        "actions": actions,
        "read_only": clean_action == "report",
        "idempotent": True,
        "redacted": True,
        "secret_values_included": False,
    }


_REVIEW_APPLY_DIR_NAME = "review_apply"
_REVIEW_DISCARD_DIR_NAME = "review_discard"
_EMPTY_DIFF_SENTINEL = "(empty diff against base)"
_REVIEW_RECOVERY_STATES = frozenset(
    {"failed", "verify_failed", "candidate_rejected", "interrupted"}
)


def _review_marker_path(paths: RunPaths, dir_name: str, task_id: str) -> Path:
    return paths.execution_dir / dir_name / f"{task_id}.json"


def _read_json_marker(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_marker(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _harvest_meta(paths: RunPaths, task_id: str) -> dict[str, Any] | None:
    return _read_json_marker(_harvest_dir(paths) / f"{task_id}.json")


def _untracked_note(untracked: list[str]) -> str | None:
    if not untracked:
        return None
    preview = ", ".join(untracked[:10])
    if len(untracked) > 10:
        preview += ", ..."
    return f"untracked files created: {preview}"


def harvest_ready_review_diffs(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    base_branch: str | None = None,
) -> list[dict[str, Any]]:
    """Produce per-task review diffs for every ready_for_merge task worktree."""
    paths = record.paths
    harvested: list[dict[str, Any]] = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if canonical_task_status(str(task.get("status") or "")) != "ready_for_merge":
            continue
        entry = _classify_task(paths, task)
        if not entry["worktree_present"]:
            continue
        harvested.append(_harvest_task(paths, entry, base_branch=base_branch or ""))
    return harvested


def _recovery_offer(task: dict[str, Any], state: str) -> dict[str, Any] | None:
    if state not in _REVIEW_RECOVERY_STATES:
        return None
    task_id = str(task.get("id") or "")
    title = str(redact_secrets(str(task.get("title") or ""))).strip()
    scope = [
        str(item).strip()
        for item in (task.get("write_scope") or task.get("estimated_files") or [])
        if str(item).strip()
    ]
    offer: dict[str, Any] = {
        "kind": "regenerate_subtree",
        "start_method": "forge.plan.regenerate.start",
        "suggested_params": {
            "instruction": str(
                redact_secrets(
                    f"Regenerate the plan subtree for task {task_id}"
                    + (f" ({title})" if title else "")
                    + f"; the previous attempt ended {state}."
                )
            ),
            "focus": str(redact_secrets(", ".join(scope[:5]))) or None,
        },
    }
    if state == "interrupted":
        offer["harvest_method"] = "forge.swarm.reconcile"
    return offer


def forge_swarm_review_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Read-only per-task review listing for the review merge strategy."""
    paths = record.paths
    root_name = forge_artifact_root_name(record.plan_id)
    items: list[dict[str, Any]] = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        entry = _classify_task(paths, task)
        applied_marker = _read_json_marker(
            _review_marker_path(paths, _REVIEW_APPLY_DIR_NAME, task_id)
        )
        discarded_marker = _read_json_marker(
            _review_marker_path(paths, _REVIEW_DISCARD_DIR_NAME, task_id)
        )
        state = str(entry["state"])
        if applied_marker is not None:
            state = "applied"
        elif discarded_marker is not None:
            state = "discarded"
        meta = _harvest_meta(paths, task_id)
        untracked = [str(item) for item in ((meta or {}).get("untracked_files") or [])]
        diff_path = _harvest_dir(paths) / f"{task_id}.diff"
        items.append(
            {
                "task_id": task_id,
                "title": str(redact_secrets(str(task.get("title") or ""))),
                "status": str(entry["status"]),
                "state": state,
                "reviewable": state == "ready_for_merge",
                "diff_available": bool(entry["diff_available"]),
                "diff_artifact_id": (
                    f"{root_name}:execution/{_HARVEST_DIR_NAME}/{task_id}.diff"
                    if diff_path.exists()
                    else None
                ),
                "untracked_files": untracked,
                "untracked_files_note": _untracked_note(untracked),
                "worktree_present": bool(entry["worktree_present"]),
                "applied": applied_marker is not None,
                "discarded": discarded_marker is not None,
                "recovery": _recovery_offer(task, state),
            }
        )
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item["state"])] = counts.get(str(item["state"]), 0) + 1
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "items": items,
        "state_counts": counts,
        "pending_review_task_ids": [item["task_id"] for item in items if item["reviewable"]],
        "apply_method": "forge.swarm.apply",
        "discard_method": "forge.swarm.discard",
        "working_tree_untouched_until_apply": True,
        "redacted": True,
        "secret_values_included": False,
    }


def _ordered_requested_tasks(
    plan: dict[str, Any],
    task_ids: list[str],
) -> list[dict[str, Any]]:
    requested = {str(item).strip() for item in task_ids if str(item).strip()}
    for task_id in sorted(requested):
        if find_task(plan, task_id) is None:
            raise ProtocolError("task_not_found", f"Unknown task id: {task_id}")
    ordered = [
        task
        for task in (plan.get("tasks") or [])
        if isinstance(task, dict) and str(task.get("id") or "").strip() in requested
    ]
    return ordered


def forge_swarm_apply_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    task_ids: list[str],
    base_branch: str | None = None,
) -> dict[str, Any]:
    """Apply per-task review diffs to the canonical working tree.

    Applies are sequential in plan order, per-task atomic (``git apply
    --check`` first), and never stage or commit anything. Untracked files
    created by the worker are NOT applied (no contents in the diff) and are
    surfaced prominently per task so nothing lands silently.
    """
    paths = record.paths
    ordered = _ordered_requested_tasks(plan, task_ids)
    # Pre-validate everything before mutating anything.
    for task in ordered:
        task_id = str(task.get("id") or "").strip()
        if _read_json_marker(_review_marker_path(paths, _REVIEW_APPLY_DIR_NAME, task_id)):
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        if status != "ready_for_merge":
            raise ProtocolError(
                "task_not_reviewable",
                f"Task {task_id} is not awaiting review (status {status}).",
            )
    applied: list[dict[str, Any]] = []
    for task in ordered:
        task_id = str(task.get("id") or "").strip()
        marker_path = _review_marker_path(paths, _REVIEW_APPLY_DIR_NAME, task_id)
        existing_marker = _read_json_marker(marker_path)
        if existing_marker is not None:
            untracked = [
                str(item) for item in (existing_marker.get("untracked_files_not_applied") or [])
            ]
            applied.append(
                {
                    "task_id": task_id,
                    "applied": True,
                    "already_applied": True,
                    "untracked_files_not_applied": untracked,
                    "untracked_files_note": _untracked_note(untracked),
                }
            )
            continue
        entry = _classify_task(paths, task)
        diff_path = _harvest_dir(paths) / f"{task_id}.diff"
        if not diff_path.exists():
            if entry["worktree_present"]:
                _harvest_task(paths, entry, base_branch=base_branch or "")
            if not diff_path.exists():
                raise ProtocolError(
                    "diff_unavailable",
                    f"No review diff is available for task {task_id}; "
                    "the task worktree is gone and nothing was harvested.",
                )
        diff_text = diff_path.read_text(encoding="utf-8")
        meta = _harvest_meta(paths, task_id) or {}
        untracked = [str(item) for item in (meta.get("untracked_files") or [])]
        nothing_to_apply = diff_text.strip() in {"", _EMPTY_DIFF_SENTINEL}
        if not nothing_to_apply:
            try:
                apply_patch_file(paths.root, patch_path=diff_path)
            except GitOpsError as exc:
                raise ProtocolError(
                    "apply_failed",
                    str(
                        redact_secrets(
                            f"Applying task {task_id} diff to the working tree failed: {exc}. "
                            f"Already applied this call: "
                            f"{', '.join(str(item['task_id']) for item in applied) or '(none)'}."
                        )
                    ),
                ) from exc
        _write_json_marker(
            marker_path,
            {
                "task_id": task_id,
                "applied_at": now_iso(),
                "nothing_to_apply": nothing_to_apply,
                "diff_artifact": os.fspath(diff_path.relative_to(paths.root)),
                "untracked_files_not_applied": untracked,
            },
        )
        set_task_status(plan, task_id, "done")
        save_plan(paths, plan)
        applied.append(
            {
                "task_id": task_id,
                "applied": True,
                "already_applied": False,
                "nothing_to_apply": nothing_to_apply,
                "diff_artifact": os.fspath(diff_path.relative_to(paths.root)),
                "untracked_files_not_applied": untracked,
                "untracked_files_note": _untracked_note(untracked),
            }
        )
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "applied": applied,
        "working_tree_committed": False,
        "redacted": True,
        "secret_values_included": False,
    }


def forge_swarm_discard_result(
    record: ForgePlanRecord,
    plan: dict[str, Any],
    *,
    task_ids: list[str],
) -> dict[str, Any]:
    """Discard reviewed task work: drop the preserved worktree, keep artifacts."""
    paths = record.paths
    ordered = _ordered_requested_tasks(plan, task_ids)
    discardable_statuses = {
        "ready_for_merge",
        "interrupted",
        "failed",
        "verify_failed",
        "candidate_rejected",
    }
    for task in ordered:
        task_id = str(task.get("id") or "").strip()
        if _read_json_marker(_review_marker_path(paths, _REVIEW_DISCARD_DIR_NAME, task_id)):
            continue
        if _read_json_marker(_review_marker_path(paths, _REVIEW_APPLY_DIR_NAME, task_id)):
            raise ProtocolError(
                "task_already_applied",
                f"Task {task_id} was already applied to the working tree; "
                "revert it through source control instead of discarding.",
            )
        status = canonical_task_status(str(task.get("status") or ""))
        if status not in discardable_statuses:
            raise ProtocolError(
                "task_not_reviewable",
                f"Task {task_id} cannot be discarded (status {status}).",
            )
    discarded: list[dict[str, Any]] = []
    for task in ordered:
        task_id = str(task.get("id") or "").strip()
        marker_path = _review_marker_path(paths, _REVIEW_DISCARD_DIR_NAME, task_id)
        if _read_json_marker(marker_path) is not None:
            discarded.append({"task_id": task_id, "discarded": True, "already_discarded": True})
            continue
        entry = _classify_task(paths, task)
        worktree_result: dict[str, Any] = {"discarded": False, "reason": "already_absent"}
        if entry["worktree_present"]:
            worktree_result = _discard_task_worktree(paths, entry)
        _write_json_marker(
            marker_path,
            {
                "task_id": task_id,
                "discarded_at": now_iso(),
                "worktree_removed": bool(worktree_result.get("discarded")),
            },
        )
        set_task_status(plan, task_id, "candidate_rejected")
        task_obj = find_task(plan, task_id)
        if task_obj is not None:
            task_obj["last_error"] = "discarded in IDE swarm review"
        save_plan(paths, plan)
        discarded.append(
            {
                "task_id": task_id,
                "discarded": True,
                "already_discarded": False,
                "worktree_removed": bool(worktree_result.get("discarded")),
            }
        )
    return {
        "session_id": record.session_id,
        "plan_id": record.plan_id,
        "discarded": discarded,
        "redacted": True,
        "secret_values_included": False,
    }
