from __future__ import annotations

import io
import json
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from rich.console import Console

from .atomic_io import atomic_write_json, atomic_write_text
from .branding import env_get
from .config import AppConfig
from .conflict_auto_resolver import (
    attempt_auto_resolve_conflict,
    bump_conflict_attempt,
    can_attempt_conflict_auto_resolve,
    load_conflict_auto_resolve_settings,
)
from .error_text import sanitize_optional_error_summary
from .execution_shared import safe_task_file_component
from .failed_task_evidence import preserve_failed_task_evidence
from .failure_category import FailureCategory, failure_category_value
from .forge import (
    ForgeError,
    RunPaths,
    WorkspaceRiskLevel,
    find_task,
    format_workspace_context_summary_lines,
    load_plan,
    refresh_workspace_context_artifacts,
    save_plan,
    set_task_status,
)
from .forge_events import emit_task_status
from .git_ops import (
    GitOpsError,
    branch_exists,
    current_branch,
    delete_branch,
    has_head_commit,
    merge_no_ff,
    untracked_files,
)
from .git_worktrees import ensure_task_worktree, prune_worktrees, remove_task_worktree
from .integration_gate import (
    IntegrationGateResult,
    record_integration_failure_knowledge,
    record_integration_resolution_knowledge,
    resolve_integration_verify_mode,
    run_integration_gate,
)
from .knowledge_base import (
    find_latest_task_attempt_entry,
    load_task_attempt_entries_for_task,
    rebuild_knowledge_index,
    write_task_attempt_resolution_entry,
)
from .knowledge_capture import (
    mark_knowledge_capture_promotion_skipped,
    promote_validated_knowledge_capture,
)
from .merge_conflict_reviewer import (
    capture_merge_conflict_context,
    list_unmerged_files,
    review_merge_conflict,
    try_abort_merge,
    write_conflict_artifacts,
)
from .plan_validation import PlannerFailedError, raise_for_execution_ready_plan
from .remote_sync import (
    ensure_pr_or_mr,
    get_remote_url,
    init_remote_record,
    load_remote_settings_from_env,
    push_base,
    push_branch,
    resolve_provider,
    truncate_output,
    write_remote_record,
)
from .replanning import (
    ReplanAttemptResult,
    build_replanning_trigger,
    resolve_replanning_mode,
    run_replanning_attempt,
)
from .review_gate import ReviewError, ReviewOutcome, review_task
from .run_lock import RunMutationGuard, acquire_run_mutation_guard, workspace_mutation_run_id
from .surface.console import make_console
from .surface.rich_surface import _truncate_inline
from .swarm_backend import (
    PreparedBatchCandidateWorkspace,
    PreparedTaskWorkspace,
    SwarmBackend,
    select_swarm_backend,
)
from .swarm_scheduler import (
    SUCCESSFUL_TERMINAL_STATUSES,
    canonical_task_status,
    compute_schedule,
    is_expected_red_regression_precursor,
)
from .swarm_trace import (
    SerializedSwarmTraceSink,
    SwarmTraceSink,
    emit_swarm_trace,
    normalize_swarm_trace_level,
)
from .swarm_worker import (
    TaskWorkerResult,
    reject_abnormal_success_result,
    resolve_worker_verify_contract,
    run_task_worker,
)
from .verification_command_analysis import is_benign_non_execution_reason
from .verification_repair import TASK_STATUS_COMPLETED_UNVERIFIED
from .verify_gate import ResolvedVerifyCommands, resolve_verify_command_selection
from .workspace_binding import WorkspaceBinding

DEFAULT_SWARM_MAX_STEPS = 50
_LOCK_WAIT_TIMEOUT_ENV = "ALYSIS_FORGE_LOCK_WAIT_TIMEOUT_S"
_LOCK_POLL_INTERVAL_ENV = "ALYSIS_FORGE_LOCK_POLL_INTERVAL_S"


@dataclass(frozen=True)
class MergeOutcome:
    task_id: str
    branch: str
    success: bool
    merge_commit_hash: str | None
    error: str | None
    cleanup_error: str | None = None
    conflict_review_path: str | None = None
    backend_name: str | None = None
    action: str = "merged"
    worker_result_kind: str | None = None
    salvaged_nonzero_exit: bool = False
    salvaged_agent_exception: bool = False
    agent_exception_summary: str | None = None


@dataclass(frozen=True)
class ReadyBatchItem:
    task: dict[str, Any]
    prepared_workspace: PreparedTaskWorkspace
    changed_files: tuple[str, ...]
    result: TaskWorkerResult | None = None
    report_path_raw: str | None = None
    remote_record: dict[str, object] | None = None

    @property
    def task_id(self) -> str:
        return str(self.task.get("id") or "")

    @property
    def branch(self) -> str:
        return str(self.task.get("branch") or "")


@dataclass(frozen=True)
class SwarmRunOutcome:
    status: str
    clean: bool
    exit_code: int
    verification_status: str
    reason_codes: tuple[str, ...]
    task_status_counts: dict[str, int]
    integration_total: int
    integration_failed: int
    final_integration_batch: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "clean": self.clean,
            "exit_code": self.exit_code,
            "verification_status": self.verification_status,
            "reason_codes": list(self.reason_codes),
            "task_status_counts": dict(self.task_status_counts),
            "integration": {
                "total": self.integration_total,
                "failed": self.integration_failed,
                "final_batch": self.final_integration_batch,
            },
        }


def _result_agent_exception_summary(result: TaskWorkerResult | None) -> str | None:
    if result is None:
        return None
    return sanitize_optional_error_summary(result.agent_exception_summary)


def _worker_result_nonexecuting_verification_reason(result: TaskWorkerResult) -> str | None:
    payload = result.verify_payload
    if not isinstance(payload, dict):
        return None
    command_results = payload.get("command_results")
    if not isinstance(command_results, list):
        return None
    for item in command_results:
        if not isinstance(item, dict):
            continue
        if item.get("real_execution") is not False:
            continue
        reason = str(item.get("non_execution_reason") or "").strip()
        if str(item.get("status") or "").strip().lower() == "skipped" and (
            is_benign_non_execution_reason(reason)
        ):
            continue
        command = str(item.get("command") or item.get("effective_command") or "").strip()
        if reason and command:
            return f"{reason} ({command})"
        return reason or command or "verification command did not execute tests"
    return None


def _merge_outcome_recovery_suffix(_outcome: MergeOutcome) -> str:
    return ""


def _merge_outcome_success_trace_message(outcome: MergeOutcome) -> str:
    if outcome.action == "applied":
        return f"Applied successfully ({outcome.merge_commit_hash or 'no merge hash'})."
    if outcome.action == "noop":
        return "Accepted already-satisfied no-op outcome; no merge/apply was required."
    return f"Merged successfully ({outcome.merge_commit_hash or 'no merge hash'})."


def _normalize_relpath(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _untracked_overwrite_conflicts(*, root: Path, changed_files: list[str] | None) -> list[str]:
    changed = {
        _normalize_relpath(path) for path in (changed_files or []) if _normalize_relpath(path)
    }
    if not changed:
        return []
    try:
        untracked = {_normalize_relpath(path) for path in untracked_files(root)}
    except GitOpsError:
        return []
    return sorted(path for path in changed if path in untracked)


def _raise_if_untracked_overwrite_conflict(
    *, root: Path, task_id: str, changed_files: list[str] | None
) -> None:
    conflicts = _untracked_overwrite_conflicts(root=root, changed_files=changed_files)
    if not conflicts:
        return
    preview = ", ".join(conflicts[:20])
    if len(conflicts) > 20:
        preview += ", ..."
    raise GitOpsError(
        "blocked to protect untracked workspace files that would be overwritten by "
        f"task {task_id}: {preview}"
    )


def _candidate_branch_name(*, run_id: str, batch_label: str) -> str:
    # Keep candidate refs short and flat so snapshot-backed candidate repos can
    # create refs/heads/<branch>.lock reliably on Windows temp/worktree paths.
    safe_run_id = safe_task_file_component(run_id)
    if len(safe_run_id) <= 16:
        # Counter-based run ids (e.g. "017-wayland") are already short, and the
        # counter is the unique part — keep the whole id so tokens never collide
        # between runs that share a word.
        run_token = safe_run_id.strip("-_") or safe_run_id
    else:
        run_token = safe_run_id.rsplit("_", 1)[-1][-8:] or safe_run_id[-8:]
    safe_batch_label = safe_task_file_component(batch_label)
    batch_token = safe_batch_label.replace("batch_", "b").replace("_", "")
    return f"syc-{run_token}-{batch_token}"


def _repo_rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def ensure_swarm_dirs(paths: RunPaths) -> None:
    (paths.execution_dir / "worker_results").mkdir(parents=True, exist_ok=True)
    (paths.execution_dir / "merge_results").mkdir(parents=True, exist_ok=True)
    paths.execution_integration_dir.mkdir(parents=True, exist_ok=True)
    (paths.execution_dir / "remote").mkdir(parents=True, exist_ok=True)
    (paths.execution_dir / "trace").mkdir(parents=True, exist_ok=True)
    (paths.run_dir / "worktrees").mkdir(parents=True, exist_ok=True)


def _default_swarm_trace_path(paths: RunPaths) -> Path:
    return paths.execution_dir / "trace" / "swarm_trace.jsonl"


@dataclass(frozen=True)
class _CompositeMutationGuard:
    workspace_guard: RunMutationGuard
    run_guard: RunMutationGuard

    @property
    def run_id(self) -> str:
        return self.run_guard.run_id

    @property
    def mode(self) -> str:
        return self.run_guard.mode

    @property
    def run_dir(self) -> Path:
        return self.run_guard.run_dir

    @property
    def workspace_root(self) -> Path:
        return self.run_guard.workspace_root

    @property
    def owner_token(self) -> str:
        return self.run_guard.owner_token

    @property
    def lock_path(self) -> Path:
        return self.run_guard.lock_path

    @property
    def recovery_path(self) -> Path:
        return self.run_guard.recovery_path

    @property
    def acquired_after_wait(self) -> bool:
        return self.workspace_guard.acquired_after_wait or self.run_guard.acquired_after_wait

    @property
    def wait_started_at(self) -> str | None:
        return self.workspace_guard.wait_started_at or self.run_guard.wait_started_at

    @property
    def wait_finished_at(self) -> str | None:
        return self.workspace_guard.wait_finished_at or self.run_guard.wait_finished_at

    def release(self) -> None:
        self.run_guard.release()
        self.workspace_guard.release()


def _env_float(name: str) -> float | None:
    raw = env_get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def acquire_swarm_mutation_guard(
    paths: RunPaths,
    *,
    mode: str = "forge_swarm",
    wait: bool = True,
    wait_timeout_s: float | None = None,
    poll_interval_s: float | None = None,
    on_wait: Callable[[dict[str, Any]], None] | None = None,
    owner_session_id: str | None = None,
) -> RunMutationGuard | _CompositeMutationGuard:
    effective_wait_timeout_s = (
        _env_float(_LOCK_WAIT_TIMEOUT_ENV) if wait_timeout_s is None else wait_timeout_s
    )
    effective_poll_interval_s = (
        _env_float(_LOCK_POLL_INTERVAL_ENV) if poll_interval_s is None else poll_interval_s
    )
    if effective_poll_interval_s is None:
        effective_poll_interval_s = 1.0
    workspace_guard = acquire_run_mutation_guard(
        run_id=workspace_mutation_run_id(paths.root),
        mode=f"{mode}:workspace",
        run_dir=paths.runtime_dir / "workspace_execution",
        workspace_root=paths.root,
        wait=wait,
        wait_timeout_s=effective_wait_timeout_s,
        poll_interval_s=effective_poll_interval_s,
        on_wait=on_wait,
        owner_session_id=owner_session_id,
    )
    try:
        run_guard = acquire_run_mutation_guard(
            run_id=paths.run_id,
            mode=mode,
            run_dir=paths.run_dir,
            workspace_root=paths.root,
            wait=wait,
            wait_timeout_s=effective_wait_timeout_s,
            poll_interval_s=effective_poll_interval_s,
            on_wait=on_wait,
            owner_session_id=owner_session_id,
        )
    except Exception:
        workspace_guard.release()
        raise
    return _CompositeMutationGuard(workspace_guard=workspace_guard, run_guard=run_guard)


def _write_concurrency_revalidation_artifact(
    *,
    paths: RunPaths,
    guard: RunMutationGuard | _CompositeMutationGuard,
    plan_changed: bool,
) -> None:
    payload = {
        "schema_version": 1,
        "reason_code": "queued_execution_revalidated",
        "run_id": paths.run_id,
        "workspace_root": os.fspath(paths.root),
        "acquired_after_wait": bool(getattr(guard, "acquired_after_wait", False)),
        "wait_started_at": getattr(guard, "wait_started_at", None),
        "wait_finished_at": getattr(guard, "wait_finished_at", None),
        "plan_reloaded": True,
        "plan_changed": plan_changed,
    }
    atomic_write_json(paths.execution_dir / "concurrency_revalidation.json", payload)


def _write_merge_result(paths: RunPaths, outcome: MergeOutcome) -> Path:
    out_dir = paths.execution_dir / "merge_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{outcome.task_id}.json"
    payload = {
        "task_id": outcome.task_id,
        "branch": outcome.branch,
        "success": outcome.success,
        "merge_commit_hash": outcome.merge_commit_hash,
        "error": outcome.error,
        "cleanup_error": outcome.cleanup_error,
        "conflict_review_path": outcome.conflict_review_path,
        "backend_name": outcome.backend_name,
        "action": outcome.action,
        "worker_result_kind": outcome.worker_result_kind,
        "salvaged_nonzero_exit": outcome.salvaged_nonzero_exit,
        "salvaged_agent_exception": outcome.salvaged_agent_exception,
        "agent_exception_summary": outcome.agent_exception_summary,
    }
    atomic_write_json(out_path, payload)
    return out_path


def _persist_worker_result(paths: RunPaths, result: TaskWorkerResult) -> Path:
    out_dir = paths.execution_dir / "worker_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{safe_task_file_component(result.task_id)}.json"
    atomic_write_json(out, result.to_json())
    return out


def _load_worker_changed_files(paths: RunPaths, task_id: str) -> list[str]:
    result_path = (
        paths.execution_dir / "worker_results" / f"{safe_task_file_component(task_id)}.json"
    )
    if not result_path.exists():
        return []
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    raw = payload.get("changed_files")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _load_worker_result_payload(paths: RunPaths, task_id: str) -> dict[str, Any] | None:
    result_path = (
        paths.execution_dir / "worker_results" / f"{safe_task_file_component(task_id)}.json"
    )
    if not result_path.exists():
        return None
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _worker_knowledge_capture_artifact_dir(
    paths: RunPaths,
    *,
    task_id: str,
    result: TaskWorkerResult | None = None,
) -> Path | None:
    raw = result.knowledge_capture_artifact_dir if result is not None else None
    if not raw:
        payload = _load_worker_result_payload(paths, task_id)
        raw = str((payload or {}).get("knowledge_capture_artifact_dir") or "").strip() or None
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else paths.root / candidate


def _mark_worker_knowledge_capture_skipped(
    paths: RunPaths,
    *,
    task_id: str,
    reason: str,
    result: TaskWorkerResult | None = None,
) -> None:
    artifact_dir = _worker_knowledge_capture_artifact_dir(paths, task_id=task_id, result=result)
    if artifact_dir is None:
        return
    mark_knowledge_capture_promotion_skipped(
        artifact_dir=artifact_dir,
        reason=reason,
    )


def _promote_worker_knowledge_capture(
    paths: RunPaths,
    *,
    plan: dict[str, Any],
    task_id: str,
    result: TaskWorkerResult | None = None,
):
    artifact_dir = _worker_knowledge_capture_artifact_dir(paths, task_id=task_id, result=result)
    if artifact_dir is None:
        return None
    task = find_task(plan, task_id)
    if task is None:
        return None
    return promote_validated_knowledge_capture(
        paths=paths,
        task=task,
        artifact_dir=artifact_dir,
    )


def _worker_result_artifact_path(
    paths: RunPaths,
    *,
    task_id: str,
    field: str,
    result: TaskWorkerResult | None = None,
) -> Path | None:
    raw = str(getattr(result, field, "") or "").strip() if result is not None else ""
    if not raw:
        payload = _load_worker_result_payload(paths, task_id)
        raw = str((payload or {}).get(field) or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else paths.root / candidate


def _worker_task_attempt_entry(
    paths: RunPaths,
    *,
    task_id: str,
    result: TaskWorkerResult | None = None,
):
    entry_id = (
        str(getattr(result, "task_attempt_entry_id", "") or "").strip()
        if result is not None
        else ""
    )
    if not entry_id:
        payload = _load_worker_result_payload(paths, task_id)
        entry_id = str((payload or {}).get("task_attempt_entry_id") or "").strip()
    if entry_id:
        for entry in load_task_attempt_entries_for_task(paths=paths, task_id=task_id):
            if entry.id == entry_id:
                return entry
    return find_latest_task_attempt_entry(
        paths=paths,
        task_id=task_id,
        source="swarm_worker",
        include_resolution_entries=False,
    )


def _resolve_worker_task_attempt_acceptance(
    paths: RunPaths,
    *,
    plan: dict[str, Any],
    task_id: str,
    acceptance_state: str,
    summary: str,
    result: TaskWorkerResult | None = None,
) -> bool:
    task = find_task(plan, task_id)
    if task is None:
        return False
    attempt_entry = _worker_task_attempt_entry(paths, task_id=task_id, result=result)
    if attempt_entry is None or attempt_entry.resolves:
        return False
    changed_files = (
        list(result.changed_files)
        if result is not None
        else _load_worker_changed_files(paths, task_id)
    )
    verify_summary = getattr(result, "verify_summary", None) if result is not None else None
    budget_path = paths.execution_budgets_dir / f"{safe_task_file_component(task_id)}.json"
    resolution_entry = write_task_attempt_resolution_entry(
        paths=paths,
        task=task,
        source="swarm_orchestrator",
        acceptance_state=acceptance_state,
        resolved_attempt_id=attempt_entry.id,
        summary=summary,
        changed_files=changed_files,
        verify_summary=verify_summary,
        report_path=_worker_result_artifact_path(
            paths, task_id=task_id, field="report_path", result=result
        ),
        patch_path=_worker_result_artifact_path(
            paths, task_id=task_id, field="patch_path", result=result
        ),
        verify_artifact_path=_worker_result_artifact_path(
            paths,
            task_id=task_id,
            field="verify_artifact_path",
            result=result,
        ),
        budget_artifact_path=budget_path if budget_path.exists() else None,
        session_artifact_dir=None,
        extra_tags=["swarm", "acceptance"],
    )
    return bool(resolution_entry.file_path)


def _remote_record_lines(record: dict[str, object]) -> list[str]:
    lines = [
        f"remote={record.get('remote') or '-'} provider={record.get('provider') or 'unknown'}",
        (
            f"pushed_branch={record.get('pushed_branch')} "
            f"pushed_base={record.get('pushed_base')} "
            f"created_pr={record.get('created_pr')}"
        ),
    ]
    pr_url = str(record.get("pr_url") or "").strip()
    if pr_url:
        lines.append(f"pr_url={pr_url}")
    pr_id = str(record.get("pr_number_or_iid") or "").strip()
    if pr_id:
        lines.append(f"pr_number_or_iid={pr_id}")
    for key in ("branch_push_output", "base_push_output", "pr_output"):
        val = str(record.get(key) or "").strip()
        if val:
            lines.append(f"{key}={truncate_output(val, max_chars=300)}")
    raw_errors = record.get("errors")
    if isinstance(raw_errors, list):
        for err in raw_errors:
            msg = str(err).strip()
            if msg:
                lines.append(f"error={truncate_output(msg, max_chars=300)}")
    return lines


def _append_remote_report_update(
    *,
    paths: RunPaths,
    report_path_raw: str,
    record: dict[str, object],
) -> None:
    candidate = Path(report_path_raw)
    if not candidate.is_absolute():
        candidate = paths.root / candidate
    if not candidate.exists():
        return
    update_lines = ["", "## Remote Sync Update", ""]
    update_lines.extend(f"- {line}" for line in _remote_record_lines(record))
    text = candidate.read_text(encoding="utf-8")
    atomic_write_text(candidate, text.rstrip() + "\n" + "\n".join(update_lines) + "\n")


def _swarm_binding_metadata(
    *,
    paths: RunPaths,
    workspace_binding: WorkspaceBinding | None,
) -> dict[str, object]:
    if workspace_binding is not None:
        workspace_context = workspace_binding.workspace_context
        return {
            "requested_path": workspace_binding.requested_path,
            "workspace_root": workspace_context.workspace_root,
            "focus_dir": workspace_context.focus_path,
            "binding_source": workspace_binding.binding_source,
            "binding_risk_level": workspace_binding.risk_level,
            "binding_risk_reasons": workspace_binding.risk_reasons,
            "broad_workspace_override_used": workspace_binding.broad_workspace_override_used,
        }
    return {
        "requested_path": paths.binding_requested_path or paths.focus_path or paths.root,
        "workspace_root": paths.root,
        "focus_dir": paths.focus_path or paths.root,
        "binding_source": paths.binding_source or "current_run_pointer",
        "binding_risk_level": paths.binding_risk_level or WorkspaceRiskLevel.HEALTHY,
        "binding_risk_reasons": paths.binding_risk_reasons,
        "broad_workspace_override_used": paths.binding_broad_workspace_override_used,
    }


def _swarm_binding_summary_lines(
    *,
    paths: RunPaths,
    workspace_binding: WorkspaceBinding | None,
) -> list[str]:
    metadata = _swarm_binding_metadata(paths=paths, workspace_binding=workspace_binding)
    return [
        f"Requested Path: `{metadata['requested_path']}`",
        f"Workspace Root: `{metadata['workspace_root']}`",
        f"Focus Directory: `{metadata['focus_dir']}`",
        f"Binding Source: `{metadata['binding_source']}`",
        f"Binding Risk Level: `{metadata['binding_risk_level']}`",
        (
            "Broad Workspace Override Used: "
            f"`{'yes' if metadata['broad_workspace_override_used'] else 'no'}`"
        ),
    ]


def _integration_phase_label(phase: str) -> str:
    if phase == "pre_merge_candidate":
        return "pre-merge candidate"
    if phase == "final_repo":
        return "final repo"
    return "post-merge"


_FAILURE_LIKE_STATUSES = frozenset(
    {
        "failed",
        "verify_failed",
        "candidate_rejected",
        "changes_requested",
        "merge_conflict",
        "blocked_integration",
        "interrupted",
        "cancelled",
    }
)
_NON_EXECUTABLE_SUCCESS_STATUSES = frozenset(
    {*SUCCESSFUL_TERMINAL_STATUSES, "superseded", "invalidated"}
)


def _task_status_counts(plan: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _compute_swarm_run_outcome(
    *,
    plan: dict[str, Any],
    integration_results: list[IntegrationGateResult],
    worker_verification_warnings: bool,
    review_blocked: bool,
    remote_blocked_any: bool,
    integration_blocked: bool,
    only: str | None,
    max_tasks: int | None,
    dry_run: bool,
    interrupted: bool = False,
    interrupted_task_ids: tuple[str, ...] = (),
    review_merge_strategy: bool = False,
) -> SwarmRunOutcome:
    counts = _task_status_counts(plan)
    statuses = list(counts)
    failed_integration = [item for item in integration_results if not item.passed]
    final_integration = next(
        (item for item in reversed(integration_results) if item.phase == "final_repo"),
        None,
    )
    reason_codes: list[str] = []
    exit_code = 0
    status = "clean"
    verification_status = "passed" if integration_results else "not_run"

    if review_blocked:
        reason_codes.append("review_blocked")
    if remote_blocked_any:
        reason_codes.append("remote_blocked")
    if integration_blocked:
        reason_codes.append("integration_blocked")
    if review_blocked or remote_blocked_any or integration_blocked:
        exit_code = 1
        status = "failed"

    if any(item in _FAILURE_LIKE_STATUSES for item in statuses):
        exit_code = 1
        status = "failed"
        reason_codes.append("task_failed_or_blocked")

    acceptable_end_statuses = set(_NON_EXECUTABLE_SUCCESS_STATUSES)
    if review_merge_strategy:
        acceptable_end_statuses.add("ready_for_merge")
    is_full_run = only is None and max_tasks is None and not dry_run
    if is_full_run and any(item not in acceptable_end_statuses for item in statuses):
        exit_code = 1
        status = "incomplete"
        reason_codes.append("tasks_remaining")

    if final_integration is not None:
        if final_integration.passed:
            verification_status = "passed"
        elif final_integration.mode == "warn":
            verification_status = "failed_tolerated_by_warn_policy"
            status = "integration_failed_warn_mode"
            exit_code = 1
            reason_codes.append("final_integration_failed_warn_mode")
        else:
            verification_status = "failed_blocking"
            status = "failed"
            exit_code = 1
            reason_codes.append("final_integration_failed")
    elif failed_integration and status == "clean":
        if all(item.mode == "warn" for item in failed_integration):
            verification_status = "failed_tolerated_by_warn_policy"
            status = "completed_with_verification_warnings"
            reason_codes.append("integration_failed_warn_mode")
        else:
            verification_status = "failed_blocking"
            status = "failed"
            exit_code = 1
            reason_codes.append("integration_failed")
    elif failed_integration:
        verification_status = (
            "failed_tolerated_by_warn_policy"
            if all(item.mode == "warn" for item in failed_integration)
            else "failed_blocking"
        )

    if worker_verification_warnings and status == "clean":
        verification_status = "failed_tolerated_by_warn_policy"
        status = "completed_with_verification_warnings"
        reason_codes.append("worker_verification_failed_warn_mode")
    elif worker_verification_warnings and verification_status == "not_run":
        verification_status = "failed_tolerated_by_warn_policy"
        reason_codes.append("worker_verification_failed_warn_mode")

    if review_merge_strategy and counts.get("ready_for_merge") and status == "clean":
        status = "review_pending"
        reason_codes.append("merge_review_pending")

    if interrupted:
        status = "interrupted"
        exit_code = 130
        reason_codes.append("cancelled_cooperatively")
        if interrupted_task_ids:
            reason_codes.append("tasks_interrupted")

    clean = status == "clean" and exit_code == 0 and verification_status in {"passed", "not_run"}
    return SwarmRunOutcome(
        status=status,
        clean=clean,
        exit_code=exit_code,
        verification_status=verification_status,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        task_status_counts=counts,
        integration_total=len(integration_results),
        integration_failed=len(failed_integration),
        final_integration_batch=(
            final_integration.batch_label if final_integration is not None else None
        ),
    )


def _write_swarm_summary_json(
    *,
    paths: RunPaths,
    outcome: SwarmRunOutcome,
    integration_results: list[IntegrationGateResult],
) -> Path:
    summary_json_path = paths.execution_dir / "swarm_summary.json"
    payload = outcome.to_payload()
    payload["run_id"] = paths.run_id
    integration_payload = payload.get("integration")
    if isinstance(integration_payload, dict):
        integration_payload["results"] = [
            {
                "batch_label": item.batch_label,
                "phase": item.phase,
                "mode": item.mode,
                "passed": item.passed,
                "summary_path": _repo_rel(paths.root, item.summary_path),
                "merged_task_ids": list(item.merged_task_ids),
            }
            for item in integration_results
        ]
    atomic_write_json(summary_json_path, payload)
    return summary_json_path


def _write_swarm_summary(
    *,
    paths: RunPaths,
    backend_name: str,
    base_branch: str,
    executed: list[str],
    merge_outcomes: list[MergeOutcome],
    integration_results: list[IntegrationGateResult],
    worker_verification_warnings: dict[str, str] | None = None,
    replanning_results: list[ReplanAttemptResult],
    skipped: dict[str, str],
    recovered: dict[str, str],
    startup_warnings: list[str],
    dry_run: bool,
    schedule_preview: list[list[str]],
    workspace_summary_lines: list[str],
    binding_summary_lines: list[str],
    run_outcome: SwarmRunOutcome | None = None,
) -> Path:
    summary_path = paths.execution_dir / "swarm_summary.md"
    lines = [
        "# Swarm Summary",
        "",
        f"- Run ID: `{paths.run_id}`",
        f"- Backend: `{backend_name}`",
        f"- Base Branch: `{base_branch}`",
        f"- Dry Run: `{'yes' if dry_run else 'no'}`",
    ]
    if run_outcome is not None:
        lines.extend(
            [
                f"- Run Status: `{run_outcome.status}`",
                f"- Clean: `{'yes' if run_outcome.clean else 'no'}`",
                f"- Verification Status: `{run_outcome.verification_status}`",
            ]
        )
    lines.extend(["", "## Workspace Context", ""])
    if workspace_summary_lines:
        lines.extend(f"- {line}" for line in workspace_summary_lines)
    else:
        lines.append("- (not available)")

    lines.extend(["", "## Workspace Binding", ""])
    if binding_summary_lines:
        lines.extend(f"- {line}" for line in binding_summary_lines)
    else:
        lines.append("- (not available)")

    lines.extend(
        [
            "",
            "## Batches",
            "",
        ]
    )
    if schedule_preview:
        for idx, batch in enumerate(schedule_preview, start=1):
            lines.append(f"- Batch {idx}: {', '.join(batch)}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Executed Tasks", ""])
    if executed:
        for task_id in executed:
            lines.append(f"- `{task_id}`")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Merge Outcomes", ""])
    if merge_outcomes:
        for item in merge_outcomes:
            if item.success:
                if item.action == "applied":
                    merged_line = (
                        f"- `{item.task_id}` applied from snapshot workspace (`{item.branch}`), "
                        f"commit `{item.merge_commit_hash}`"
                    )
                elif item.action == "noop":
                    merged_line = (
                        f"- `{item.task_id}` completed as an already-satisfied no-op "
                        f"(`{item.branch}`); no merge required"
                    )
                else:
                    merged_line = (
                        f"- `{item.task_id}` merged (`{item.branch}`), "
                        f"commit `{item.merge_commit_hash}`"
                    )
                merged_line += _merge_outcome_recovery_suffix(item)
                if item.cleanup_error:
                    merged_line += f" (cleanup warning: {item.cleanup_error})"
                lines.append(merged_line)
            else:
                if item.action == "candidate_rejected":
                    lines.append(
                        f"- `{item.task_id}` rejected before merge/apply (`{item.branch}`): {item.error}"
                    )
                    continue
                verb = "apply" if item.action == "applied" else "merge"
                lines.append(f"- `{item.task_id}` failed {verb} (`{item.branch}`): {item.error}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Integration Gates", ""])
    if integration_results:
        for item in integration_results:
            status = "passed" if item.passed else "failed"
            phase = _integration_phase_label(item.phase)
            lines.append(
                f"- `{item.batch_label}` {phase} {status} ({item.mode}); batch tasks: "
                f"{', '.join(item.merged_task_ids) or '(none)'}; summary: "
                f"`{_repo_rel(paths.root, item.summary_path)}`"
            )
    else:
        lines.append("- (none)")

    lines.extend(["", "## Worker Verification Warnings", ""])
    if worker_verification_warnings:
        for task_id, reason in sorted(worker_verification_warnings.items()):
            lines.append(f"- `{task_id}`: {reason}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Replanning", ""])
    if replanning_results:
        for item in replanning_results:
            lines.append(f"- {item.summary_line(root=paths.root)}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Skipped", ""])
    if skipped:
        for task_id, reason in sorted(skipped.items()):
            lines.append(f"- `{task_id}`: {reason}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Recovered", ""])
    if recovered:
        for task_id, reason in sorted(recovered.items()):
            lines.append(f"- `{task_id}`: {reason}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Startup Warnings", ""])
    if startup_warnings:
        for warning in startup_warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Next Steps", ""])
    if any(not m.success and m.action == "candidate_rejected" for m in merge_outcomes):
        lines.append(
            f"- Review pre-merge batch verification artifacts under `{_repo_rel(paths.root, paths.execution_integration_dir)}`."
        )
        lines.append("- Fix the combined batch state before rerunning `alysis forge swarm`.")
    elif any(not m.success for m in merge_outcomes):
        lines.append("- Resolve merge conflicts manually and rerun `alysis forge swarm`.")
        for outcome in merge_outcomes:
            if not outcome.success and outcome.conflict_review_path:
                lines.append(f"- Review conflict report: `{outcome.conflict_review_path}`")
    elif (
        run_outcome is not None
        and not run_outcome.clean
        and run_outcome.verification_status.startswith("failed")
    ):
        lines.append(
            f"- Review integration artifacts under `{_repo_rel(paths.root, paths.execution_integration_dir)}`."
        )
        lines.append(
            f"- Review open integration issues: `{_repo_rel(paths.root, paths.execution_integration_issues_path)}`."
        )
        if replanning_results:
            lines.append(
                f"- Review replanning artifacts under `{_repo_rel(paths.root, paths.plan_replans_dir)}`."
            )
    elif any(not item.passed for item in integration_results):
        lines.append(
            f"- Review integration artifacts under `{_repo_rel(paths.root, paths.execution_integration_dir)}`."
        )
        lines.append(
            f"- Review open integration issues: `{_repo_rel(paths.root, paths.execution_integration_issues_path)}`."
        )
    else:
        lines.append("- Review reports under `.alysis/runs/<run_id>/execution/reports/`.")
    atomic_write_text(summary_path, "\n".join(lines).rstrip() + "\n")
    return summary_path


def _compose_summary_skipped(
    *,
    scheduler_skipped: dict[str, str],
    execution_skipped: dict[str, str],
    observed_task_ids: set[str],
) -> dict[str, str]:
    summary_skipped = {
        task_id: reason
        for task_id, reason in scheduler_skipped.items()
        if task_id not in observed_task_ids
    }
    summary_skipped.update(execution_skipped)
    return summary_skipped


def _parse_only(only: str | None) -> set[str] | None:
    if only is None:
        return None
    ids = {part.strip() for part in only.split(",") if part.strip()}
    return ids or None


def _mark_status(paths: RunPaths, plan: dict[str, Any], task_id: str, status: str) -> None:
    set_task_status(plan, task_id, status)
    save_plan(paths, plan)
    # Every swarm task transition goes through here, so this is the one place that has
    # to report task lifecycle events. No-op unless a --machine command is running.
    emit_task_status(task_id, status, {"run_id": paths.run_id, "source": "swarm"})


def _bump_attempt(paths: RunPaths, plan: dict[str, Any], task_id: str) -> None:
    task = find_task(plan, task_id)
    if task is None:
        return
    raw = task.get("attempts")
    try:
        attempts = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        attempts = 0
    if attempts < 0:
        attempts = 0
    task["attempts"] = attempts + 1
    save_plan(paths, plan)


def _mark_remaining_tasks_blocked_by_integration(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    reason: str,
    retry_failed: bool,
    retry_changes_requested: bool,
    parallel: int,
    max_attempts: int | None,
    only_ids: set[str] | None,
) -> list[str]:
    blocked: list[str] = []
    terminal_statuses = {
        "done",
        TASK_STATUS_COMPLETED_UNVERIFIED,
        "failed",
        "verify_failed",
        "candidate_rejected",
        "changes_requested",
        "merge_conflict",
        "blocked_integration",
        "interrupted",
        "cancelled",
    }
    changed = False
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        if only_ids is not None and task_id not in only_ids:
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        if status in terminal_statuses:
            continue
        task["status"] = "blocked_integration"
        task["last_error"] = reason
        blocked.append(task_id)
        changed = True
    if changed:
        save_plan(paths, plan)
    return blocked


def _task_has_unfinished_direct_dependent(plan: dict[str, Any], task_id: str) -> bool:
    unfinished_statuses = {"planned", "todo", "in_progress"}
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        deps = [str(dep).strip() for dep in (task.get("dependencies") or []) if str(dep).strip()]
        if task_id not in deps:
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        if status in unfinished_statuses:
            return True
    return False


def _ready_items_have_unfinished_dependents(
    *,
    plan: dict[str, Any],
    ready_items: list[ReadyBatchItem],
) -> bool:
    return any(_task_has_unfinished_direct_dependent(plan, item.task_id) for item in ready_items)


def _should_defer_red_regression_ready_task(plan: dict[str, Any], task: dict[str, Any]) -> bool:
    task_id = str(task.get("id") or "").strip()
    return bool(
        task_id
        and is_expected_red_regression_precursor(task)
        and _task_has_unfinished_direct_dependent(plan, task_id)
    )


def _deferred_red_dependency_tasks_for_ready_items(
    *,
    plan: dict[str, Any],
    ready_items: list[ReadyBatchItem],
) -> list[dict[str, Any]]:
    wanted: set[str] = set()
    for item in ready_items:
        wanted.update(
            str(dep).strip() for dep in (item.task.get("dependencies") or []) if str(dep).strip()
        )
    if not wanted:
        return []
    existing = {item.task_id for item in ready_items}
    out: list[dict[str, Any]] = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if task_id not in wanted or task_id in existing:
            continue
        if canonical_task_status(str(task.get("status") or "")) != "ready_for_merge":
            continue
        if not is_expected_red_regression_precursor(task):
            continue
        out.append(task)
    return out


def _recover_stale_in_progress(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
) -> dict[str, str]:
    recovered: dict[str, str] = {}
    changed = False
    for task in plan.get("tasks") or []:
        status = canonical_task_status(str(task.get("status") or ""))
        if status != "in_progress":
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        reason = (
            "Recovered stale in_progress task before swarm run "
            "(previous run may have been interrupted)."
        )
        task["status"] = "failed"
        task["last_error"] = reason
        recovered[task_id] = reason
        changed = True
    if changed:
        save_plan(paths, plan)
    return recovered


def _reject_ready_batch_items(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    backend: SwarmBackend,
    keep_worktrees: bool,
    ready_items: list[ReadyBatchItem],
    reason: str,
    merge_outcomes: list[MergeOutcome],
    execution_skipped: dict[str, str],
    cleanup_nonmerged_workspace: Callable[..., None],
) -> bool:
    task_attempt_resolutions = False
    changed = False
    for item in ready_items:
        task = find_task(plan, item.task_id)
        if task is not None:
            task["status"] = "candidate_rejected"
            task.pop("merge_commit_hash", None)
            existing_error = str(task.get("last_error") or "").strip()
            task["last_error"] = (
                reason
                if not existing_error or reason in existing_error
                else f"{existing_error}; {reason}"
            )
            changed = True
        execution_skipped[item.task_id] = reason
        task_attempt_resolutions = (
            _resolve_worker_task_attempt_acceptance(
                paths,
                plan=plan,
                task_id=item.task_id,
                acceptance_state="rejected",
                summary=f"Worker result was rejected because {reason}",
                result=item.result,
            )
            or task_attempt_resolutions
        )
        _mark_worker_knowledge_capture_skipped(
            paths,
            task_id=item.task_id,
            result=item.result,
            reason=f"worker result was not accepted because {reason}",
        )
        if item.remote_record is not None and item.report_path_raw:
            _append_remote_report_update(
                paths=paths,
                report_path_raw=item.report_path_raw,
                record=item.remote_record,
            )
        cleanup_nonmerged_workspace(
            item.task_id,
            item.prepared_workspace,
            reason=reason,
            result=item.result,
        )
        outcome = MergeOutcome(
            task_id=item.task_id,
            branch=item.branch,
            success=False,
            merge_commit_hash=None,
            error=reason,
            backend_name=backend.name,
            action="candidate_rejected",
            worker_result_kind=item.result.effective_result_kind
            if item.result is not None
            else None,
            salvaged_nonzero_exit=(
                item.result.salvaged_nonzero_exit if item.result is not None else False
            ),
            salvaged_agent_exception=(
                item.result.salvaged_agent_exception if item.result is not None else False
            ),
            agent_exception_summary=_result_agent_exception_summary(item.result),
        )
        _write_merge_result(paths, outcome)
        merge_outcomes.append(outcome)
    if changed:
        save_plan(paths, plan)
    return task_attempt_resolutions


def _verify_ready_batch_candidate(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    cfg: AppConfig,
    workspace_scan,
    backend: SwarmBackend,
    batch_index: int,
    base_branch: str,
    integration_mode: str,
    allow_transient_dependent_integration_warning: bool,
    integration_verify_cmd: list[str] | None,
    verify_cmd: list[str] | None,
    keep_worktrees: bool,
    ready_items: list[ReadyBatchItem],
    trace: Callable[[str, str], None],
    task_trace: Callable[[str, str], None],
    integration_runner: Callable[..., IntegrationGateResult],
) -> tuple[IntegrationGateResult | None, str | None]:
    if integration_mode == "off" or not ready_items:
        return None, None

    batch_label = f"batch_{batch_index:03d}"
    task_ids = [item.task_id for item in ready_items]
    candidate_branch = _candidate_branch_name(run_id=paths.run_id, batch_label=batch_label)
    candidate_workspace: PreparedBatchCandidateWorkspace | None = None
    cleanup_errors: list[str] = []
    result: IntegrationGateResult | None = None
    try:
        trace(
            "integration.lifecycle",
            f"Assembling pre-merge batch candidate for {batch_label}: {', '.join(task_ids)}.",
        )
        candidate_workspace = backend.prepare_candidate_workspace(
            root=paths.root,
            run_dir=paths.run_dir,
            batch_label=batch_label,
            branch=candidate_branch,
            base_branch=base_branch,
        )
        merged_paths: list[str] = []
        for item in ready_items:
            merge_title = str(item.task.get("title") or "").strip()
            merge_message = (
                f"Merge {item.task_id}: {merge_title}" if merge_title else f"Merge {item.task_id}"
            )
            backend.apply_task_to_candidate(
                root=paths.root,
                candidate_workspace=candidate_workspace,
                prepared_workspace=item.prepared_workspace,
                message=merge_message,
                changed_files=list(item.changed_files),
            )
            merged_paths.extend(item.changed_files)
        result = _run_batch_integration_gate(
            paths=paths,
            cfg=cfg,
            workspace_scan=workspace_scan,
            batch_index=batch_index,
            integration_mode=integration_mode,
            integration_verify_cmd=integration_verify_cmd,
            verify_cmd=verify_cmd,
            merged_task_ids=task_ids,
            merged_paths=merged_paths,
            root=candidate_workspace.worktree_path,
            phase="pre_merge_candidate",
            trace=trace,
            task_trace=task_trace,
            integration_runner=integration_runner,
        )
    except GitOpsError as e:
        detail = f"pre-merge batch candidate assembly failed ({batch_label}): {e}"
        trace("integration.error", detail)
        return None, detail
    finally:
        if candidate_workspace is not None:
            cleanup_errors = backend.cleanup_candidate_workspace(
                root=paths.root,
                candidate_workspace=candidate_workspace,
                keep_worktrees=keep_worktrees,
            )
            if cleanup_errors:
                trace(
                    "worktree.error",
                    (
                        f"Candidate workspace cleanup warning for {batch_label}: "
                        f"{'; '.join(cleanup_errors)}"
                    ),
                )

    if result is None:
        return None, None
    if result.passed:
        return result, None
    if (
        integration_mode == "warn"
        and allow_transient_dependent_integration_warning
        and _ready_items_have_unfinished_dependents(
            plan=plan,
            ready_items=ready_items,
        )
    ):
        trace(
            "integration.lifecycle",
            (
                f"Pre-merge batch candidate verification failed ({result.batch_label}) "
                "but at least one task has unfinished planned dependents; accepting the "
                "batch as a transient integration warning so dependent work can run."
            ),
        )
        return result, None
    reason = (
        f"pre-merge batch candidate verification failed ({result.batch_label}): {result.summary}"
    )
    if cleanup_errors:
        reason += f" Candidate cleanup warning: {'; '.join(cleanup_errors)}"
    return result, reason


def _merge_ready_batch_items_into_base(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    cfg: AppConfig,
    api_key_override: str | None,
    backend: SwarmBackend,
    base_branch: str,
    keep_worktrees: bool,
    verify_mode: str,
    verify_commands: list[str],
    verify_command_selection: ResolvedVerifyCommands | None,
    ready_items: list[ReadyBatchItem],
    merge_outcomes: list[MergeOutcome],
    trace: Callable[..., None],
    cleanup_nonmerged_workspace: Callable[..., None],
) -> tuple[list[str], list[str], list[str], bool, bool]:
    merged_task_ids: list[str] = []
    remote_task_ids: list[str] = []
    merged_paths: list[str] = []
    promoted_knowledge = False
    task_attempt_resolutions = False

    for item in ready_items:
        trace(
            "merge.lifecycle",
            f"Starting merge from branch {item.branch}.",
            task_id=item.task_id,
        )
        outcome = _merge_task(
            paths=paths,
            plan=plan,
            task=item.task,
            cfg=cfg,
            api_key_override=api_key_override,
            backend=backend,
            base_branch=base_branch,
            keep_worktrees=keep_worktrees,
            prepared_workspace=item.prepared_workspace,
            changed_files=list(item.changed_files),
            verify_commands=verify_commands,
            verify_command_selection=verify_command_selection if verify_mode != "off" else None,
            result=item.result,
        )
        merge_outcomes.append(outcome)
        if outcome.success:
            trace(
                "merge.lifecycle",
                _merge_outcome_success_trace_message(outcome),
                task_id=item.task_id,
            )
            merged_task_ids.append(item.task_id)
            merged_paths.extend(item.changed_files)
            task_attempt_resolutions = (
                _resolve_worker_task_attempt_acceptance(
                    paths,
                    plan=plan,
                    task_id=item.task_id,
                    acceptance_state="accepted",
                    summary="Worker result was accepted after successful merge/apply.",
                    result=item.result,
                )
                or task_attempt_resolutions
            )
            if item.remote_record is not None and item.report_path_raw:
                remote_task_ids.append(item.task_id)
                _append_remote_report_update(
                    paths=paths,
                    report_path_raw=item.report_path_raw,
                    record=item.remote_record,
                )
            promotion_result = _promote_worker_knowledge_capture(
                paths,
                plan=plan,
                task_id=item.task_id,
                result=item.result,
            )
            if promotion_result is not None and (
                promotion_result.fact_entry_ids or promotion_result.decision_entry_ids
            ):
                promoted_knowledge = True
            continue

        task_attempt_resolutions = (
            _resolve_worker_task_attempt_acceptance(
                paths,
                plan=plan,
                task_id=item.task_id,
                acceptance_state="rejected",
                summary=(
                    "Worker result was rejected because merge/apply failed: "
                    f"{outcome.error or 'unknown error'}"
                ),
                result=item.result,
            )
            or task_attempt_resolutions
        )
        _mark_worker_knowledge_capture_skipped(
            paths,
            task_id=item.task_id,
            result=item.result,
            reason="worker result was not accepted because merge/apply failed",
        )
        if canonical_task_status(str(item.task.get("status") or "")) == "failed":
            cleanup_nonmerged_workspace(
                item.task_id,
                item.prepared_workspace,
                reason=f"merge/apply failed: {outcome.error or 'unknown error'}",
                result=item.result,
            )
        trace(
            "merge.error",
            f"Merge failed: {outcome.error or 'unknown error'}",
            task_id=item.task_id,
        )

    return (
        merged_task_ids,
        remote_task_ids,
        merged_paths,
        promoted_knowledge,
        task_attempt_resolutions,
    )


def _merge_task(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    task: dict[str, Any],
    cfg: AppConfig,
    api_key_override: str | None,
    backend: SwarmBackend,
    base_branch: str,
    keep_worktrees: bool,
    prepared_workspace: PreparedTaskWorkspace,
    changed_files: list[str] | None = None,
    verify_commands: list[str] | None = None,
    verify_command_selection: ResolvedVerifyCommands | None = None,
    result: TaskWorkerResult | None = None,
) -> MergeOutcome:
    task_id = str(task.get("id") or "")
    branch = str(task.get("branch") or "")
    merge_title = str(task.get("title") or "").strip()
    merge_message = f"Merge {task_id}: {merge_title}" if merge_title else f"Merge {task_id}"
    try:
        if paths.has_head_commit:
            _raise_if_untracked_overwrite_conflict(
                root=paths.root,
                task_id=task_id,
                changed_files=changed_files,
            )
        apply_result = backend.apply_task_success(
            root=paths.root,
            prepared_workspace=prepared_workspace,
            message=merge_message,
            changed_files=changed_files,
        )
    except GitOpsError as e:
        unmerged_files = list_unmerged_files(prepared_workspace.control_root)
        is_merge_conflict = bool(unmerged_files)
        conflict_review_path: str | None = None
        cleanup_error: str | None = None
        if is_merge_conflict:
            context = capture_merge_conflict_context(
                prepared_workspace.control_root,
                base_branch=base_branch,
                task_branch=branch,
                merge_error=str(e),
            )
            review_outcome = review_merge_conflict(
                paths=paths,
                task=task,
                cfg=cfg,
                api_key_override=api_key_override,
                context=context,
                plan=plan,
            )
            cleanup_ok, cleanup_log = try_abort_merge(
                prepared_workspace.control_root,
                base_branch=base_branch,
            )
            conflict_artifacts = write_conflict_artifacts(
                paths=paths,
                task_id=task_id,
                context=context,
                review_json=review_outcome.review_json,
                review_md=review_outcome.review_markdown,
                cleanup_log=cleanup_log,
            )
            conflict_review_path = _repo_rel(paths.root, conflict_artifacts.review_md_path)
            if not cleanup_ok:
                cleanup_error = (
                    "merge cleanup did not fully recover repository state; "
                    f"see {_repo_rel(paths.root, conflict_artifacts.cleanup_log_path)}"
                )
            auto_settings = load_conflict_auto_resolve_settings(cfg=cfg)
            if can_attempt_conflict_auto_resolve(task=task, settings=auto_settings):
                bump_conflict_attempt(task)
                save_plan(paths, plan)
                auto_outcome = attempt_auto_resolve_conflict(
                    paths=paths,
                    plan=plan,
                    task=task,
                    cfg=cfg,
                    api_key_override=api_key_override,
                    base_branch=base_branch,
                    task_branch=branch,
                    keep_worktrees=keep_worktrees,
                    settings=auto_settings,
                    verify_commands=verify_commands,
                    verify_command_selection=verify_command_selection,
                )
                if auto_outcome.success:
                    task_obj = find_task(plan, task_id)
                    if task_obj is not None:
                        task_obj["merge_commit_hash"] = auto_outcome.merge_commit_hash
                    _mark_status(paths, plan, task_id, "done")
                    outcome = MergeOutcome(
                        task_id=task_id,
                        branch=branch,
                        success=True,
                        merge_commit_hash=auto_outcome.merge_commit_hash,
                        error=None,
                        cleanup_error=(
                            "; ".join(auto_outcome.warnings)
                            if auto_outcome.warnings
                            else cleanup_error
                        ),
                        conflict_review_path=_repo_rel(paths.root, auto_outcome.report_path),
                        backend_name=backend.name,
                        action="applied" if backend.name == "snapshot_workspace" else "merged",
                        worker_result_kind=(
                            result.effective_result_kind if result is not None else None
                        ),
                        salvaged_nonzero_exit=(
                            result.salvaged_nonzero_exit if result is not None else False
                        ),
                        salvaged_agent_exception=(
                            result.salvaged_agent_exception if result is not None else False
                        ),
                        agent_exception_summary=_result_agent_exception_summary(result),
                    )
                    _write_merge_result(paths, outcome)
                    return outcome
                auto_error = auto_outcome.error or "unknown error"
                e = GitOpsError(f"{e}; auto-resolve failed: {auto_error}")
                conflict_review_path = _repo_rel(paths.root, auto_outcome.report_path)
        _mark_status(paths, plan, task_id, "merge_conflict" if is_merge_conflict else "failed")
        outcome = MergeOutcome(
            task_id=task_id,
            branch=branch,
            success=False,
            merge_commit_hash=None,
            error=str(e),
            cleanup_error=cleanup_error,
            conflict_review_path=conflict_review_path,
            backend_name=backend.name,
            action="applied" if backend.name == "snapshot_workspace" else "merged",
            worker_result_kind=result.effective_result_kind if result is not None else None,
            salvaged_nonzero_exit=result.salvaged_nonzero_exit if result is not None else False,
            salvaged_agent_exception=(
                result.salvaged_agent_exception if result is not None else False
            ),
            agent_exception_summary=_result_agent_exception_summary(result),
        )
        _write_merge_result(paths, outcome)
        return outcome

    task_obj = find_task(plan, task_id)
    if task_obj is not None:
        task_obj["merge_commit_hash"] = apply_result.merge_commit_hash
    _mark_status(paths, plan, task_id, "done")

    cleanup_errors = backend.cleanup_task_workspace(
        root=paths.root,
        prepared_workspace=prepared_workspace,
        keep_worktrees=keep_worktrees,
    )

    outcome = MergeOutcome(
        task_id=task_id,
        branch=branch,
        success=True,
        merge_commit_hash=apply_result.merge_commit_hash,
        error=None,
        cleanup_error="; ".join(cleanup_errors) if cleanup_errors else None,
        backend_name=apply_result.backend_name,
        action=apply_result.action,
        worker_result_kind=result.effective_result_kind if result is not None else None,
        salvaged_nonzero_exit=result.salvaged_nonzero_exit if result is not None else False,
        salvaged_agent_exception=result.salvaged_agent_exception if result is not None else False,
        agent_exception_summary=_result_agent_exception_summary(result),
    )
    _write_merge_result(paths, outcome)
    return outcome


def _accept_noop_task(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    task: dict[str, Any],
    backend: SwarmBackend,
    keep_worktrees: bool,
    prepared_workspace: PreparedTaskWorkspace,
    result: TaskWorkerResult | None = None,
) -> MergeOutcome:
    task_id = str(task.get("id") or "")
    branch = str(task.get("branch") or "")
    task_obj = find_task(plan, task_id)
    if task_obj is not None:
        task_obj.pop("merge_commit_hash", None)
    _mark_status(paths, plan, task_id, "already_satisfied")
    cleanup_errors = backend.cleanup_task_workspace(
        root=paths.root,
        prepared_workspace=prepared_workspace,
        keep_worktrees=keep_worktrees,
    )
    outcome = MergeOutcome(
        task_id=task_id,
        branch=branch,
        success=True,
        merge_commit_hash=None,
        error=None,
        cleanup_error="; ".join(cleanup_errors) if cleanup_errors else None,
        backend_name=backend.name,
        action="noop",
        worker_result_kind=result.effective_result_kind if result is not None else None,
        salvaged_nonzero_exit=result.salvaged_nonzero_exit if result is not None else False,
        salvaged_agent_exception=result.salvaged_agent_exception if result is not None else False,
        agent_exception_summary=_result_agent_exception_summary(result),
    )
    _write_merge_result(paths, outcome)
    return outcome


def _run_batch_integration_gate(
    *,
    paths: RunPaths,
    cfg: AppConfig,
    workspace_scan,
    batch_index: int,
    integration_mode: str,
    integration_verify_cmd: list[str] | None,
    verify_cmd: list[str] | None,
    merged_task_ids: list[str],
    merged_paths: list[str],
    root: Path | None,
    phase: str,
    trace: Callable[[str, str], None],
    task_trace: Callable[[str, str], None],
    integration_runner: Callable[..., IntegrationGateResult],
) -> IntegrationGateResult | None:
    if integration_mode == "off" or not merged_task_ids:
        return None
    trace(
        "integration.lifecycle",
        (
            f"Running integration gate for batch {batch_index} "
            f"({phase}): {', '.join(merged_task_ids)}."
        ),
    )
    result = integration_runner(
        paths=paths,
        cfg=cfg,
        batch_index=batch_index,
        mode=integration_mode,
        merged_task_ids=merged_task_ids,
        merged_paths=merged_paths,
        integration_verify_cmd=integration_verify_cmd,
        verify_cmd=verify_cmd,
        root=root,
        repo_scan=workspace_scan,
        phase=phase,
    )
    status_text = "passed" if result.passed else "failed"
    trace(
        "integration.lifecycle",
        f"Integration gate {result.batch_label} {status_text}: {result.summary}.",
    )
    if not result.passed:
        for task_id in merged_task_ids:
            task_trace(
                task_id,
                (
                    "Pre-merge batch candidate verification failed: "
                    if phase == "pre_merge_candidate"
                    else "Integration gate failed after merge: "
                )
                + result.summary,
            )
    return result


def run_swarm(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    cfg: AppConfig,
    mode: str,
    yes: bool,
    max_steps: int | None,
    api_key_override: str | None,
    no_log: bool,
    parallel: int,
    base_branch: str | None,
    max_tasks: int | None,
    max_attempts: int | None,
    dry_run: bool,
    keep_worktrees: bool,
    retry_failed: bool,
    retry_changes_requested: bool = False,
    only: str | None,
    retry_merge_conflicts: bool,
    console: Console,
    scope_mode: str = "strict",
    verify_mode: str = "warn",
    verify_cmd: list[str] | None = None,
    integration_mode: str | None = None,
    integration_verify_cmd: list[str] | None = None,
    replanning_mode: str | None = None,
    review: bool = False,
    trace_level: str = "off",
    trace_sink: SwarmTraceSink | None = None,
    workspace_binding: WorkspaceBinding | None = None,
    worker_runner: Callable[..., TaskWorkerResult] = run_task_worker,
    review_runner: Callable[..., ReviewOutcome] = review_task,
    merge_runner: Callable[..., str] = merge_no_ff,
    integration_runner: Callable[..., IntegrationGateResult] = run_integration_gate,
    replanning_runner: Callable[..., ReplanAttemptResult] = run_replanning_attempt,
    ensure_worktree_fn: Callable[..., Any] = ensure_task_worktree,
    remove_worktree_fn: Callable[..., None] = remove_task_worktree,
    delete_branch_fn: Callable[[Path, str], None] = delete_branch,
    branch_exists_fn: Callable[[Path, str], bool] = branch_exists,
    current_branch_fn: Callable[[Path], str] = current_branch,
    run_mutation_guard: RunMutationGuard | _CompositeMutationGuard | None = None,
    cancellation_event: threading.Event | None = None,
    worker_approval_handler: Callable[..., Any] | None = None,
    merge_strategy: str = "auto",
) -> int:
    owns_run_mutation_guard = run_mutation_guard is None
    if run_mutation_guard is None:
        run_mutation_guard = acquire_swarm_mutation_guard(paths)
    try:
        ensure_swarm_dirs(paths)
        trace_level_normalized = normalize_swarm_trace_level(trace_level)
        sink = trace_sink or SerializedSwarmTraceSink(
            artifact_path=_default_swarm_trace_path(paths),
            trace_level=trace_level_normalized,
            console=console,
        )
    except Exception:
        if owns_run_mutation_guard:
            run_mutation_guard.release()
        raise

    def _trace(
        phase: str,
        message: str,
        *,
        task_id: str | None = None,
        verbosity: str = "compact",
    ) -> None:
        emit_swarm_trace(
            sink,
            run_id=paths.run_id,
            phase=phase,
            message=message,
            task_id=task_id,
            verbosity=verbosity,
        )

    try:
        if bool(getattr(run_mutation_guard, "acquired_after_wait", False)):
            previous_plan_text = json.dumps(plan, sort_keys=True, default=str)
            reloaded_plan = load_plan(paths)
            reloaded_plan_text = json.dumps(reloaded_plan, sort_keys=True, default=str)
            plan.clear()
            plan.update(reloaded_plan)
            _write_concurrency_revalidation_artifact(
                paths=paths,
                guard=run_mutation_guard,
                plan_changed=previous_plan_text != reloaded_plan_text,
            )
            _trace(
                "concurrency.revalidation",
                "Queued Forge execution acquired the workspace guard; reloaded current plan before scheduling.",
            )
        backend = select_swarm_backend(
            paths=paths,
            ensure_worktree_fn=ensure_worktree_fn,
            merge_runner=merge_runner,
            remove_worktree_fn=remove_worktree_fn,
            delete_branch_fn=delete_branch_fn,
            prune_fn=prune_worktrees,
            branch_exists_fn=branch_exists_fn,
            current_branch_fn=current_branch_fn,
            has_head_commit_fn=has_head_commit,
        )
        workspace_scan = refresh_workspace_context_artifacts(paths)
        workspace_summary_lines = format_workspace_context_summary_lines(workspace_scan)
        binding_summary_lines = _swarm_binding_summary_lines(
            paths=paths,
            workspace_binding=workspace_binding,
        )
        binding_metadata = _swarm_binding_metadata(paths=paths, workspace_binding=workspace_binding)
        effective_replanning_mode = resolve_replanning_mode(
            cfg=cfg,
            replanning_mode=replanning_mode,
        )
        _trace(
            "swarm.lifecycle",
            (
                f"Swarm starting: parallel={parallel}, dry_run={'yes' if dry_run else 'no'}, "
                f"review={'on' if review else 'off'}, verify={verify_mode}, "
                f"integration_verify={resolve_integration_verify_mode(cfg=cfg, integration_verify=integration_mode)}, "
                f"replan={effective_replanning_mode}."
            ),
        )
        _trace("swarm.backend", f"Using backend {backend.name}.")
        for line in workspace_summary_lines:
            _trace("workspace.scan", line)
        _trace(
            "workspace.binding",
            (
                "Binding: "
                f"requested={binding_metadata['requested_path']}, "
                f"workspace_root={binding_metadata['workspace_root']}, "
                f"focus_dir={binding_metadata['focus_dir']}, "
                f"risk={binding_metadata['binding_risk_level']}, "
                "broad_override="
                f"{'yes' if binding_metadata['broad_workspace_override_used'] else 'no'}."
            ),
        )
        for reason in binding_metadata["binding_risk_reasons"]:
            _trace("workspace.binding", f"Binding reason: {reason}", verbosity="full")
        if not dry_run and mode != "auto":
            msg = "swarm requires --mode auto (non-interactive)"
            _trace("swarm.error", msg)
            raise ForgeError(msg)
        if merge_strategy not in {"auto", "review"}:
            msg = f"swarm merge_strategy must be auto or review, not {merge_strategy!r}"
            _trace("swarm.error", msg)
            raise ForgeError(msg)
        if merge_strategy == "review":
            _trace(
                "merge.lifecycle",
                "Review merge strategy: completed tasks stay ready_for_merge with "
                "preserved worktrees; nothing merges into the base branch.",
            )

        selected_base = (
            base_branch.strip() if base_branch else backend.default_base_branch(paths.root)
        )
        _trace("swarm.lifecycle", f"Using base branch {selected_base}.")

        verify_commands: list[str] = []
        verify_command_selection: ResolvedVerifyCommands | None = None
        verify_commands_resolved = verify_mode == "off" or dry_run
        effective_integration_mode = resolve_integration_verify_mode(
            cfg=cfg,
            integration_verify=integration_mode,
        )

        def _get_verify_command_selection() -> ResolvedVerifyCommands:
            nonlocal verify_commands, verify_command_selection, verify_commands_resolved
            if verify_command_selection is not None:
                return verify_command_selection
            _trace("verify.lifecycle", "Resolving verify commands.", verbosity="full")
            verify_command_selection = resolve_verify_command_selection(
                cfg=cfg,
                verify_cmd=verify_cmd,
                root=paths.root,
                repo_scan=workspace_scan,
                allow_empty_config=True,
            )
            verify_commands = list(verify_command_selection.commands)
            verify_commands_resolved = True
            return verify_command_selection

        def _get_verify_commands() -> list[str]:
            nonlocal verify_commands
            if verify_commands_resolved:
                return verify_commands
            verify_commands = list(_get_verify_command_selection().commands)
            return verify_commands

        effective_max_attempts = max_attempts
        if effective_max_attempts is None and (retry_failed or retry_changes_requested):
            effective_max_attempts = 3
        only_ids = _parse_only(only)
        plan_requirements = [
            str(item).strip() for item in (plan.get("requirements") or []) if str(item).strip()
        ]
        executed: list[str] = []
        interrupted_tasks: set[str] = set()

        def _cancellation_requested() -> bool:
            return cancellation_event is not None and cancellation_event.is_set()

        merge_outcomes: list[MergeOutcome] = []
        integration_results: list[IntegrationGateResult] = []
        replanning_results: list[ReplanAttemptResult] = []
        scheduler_skipped: dict[str, str] = {}
        execution_skipped: dict[str, str] = {}
        executed_batch_history: list[list[str]] = []
        observed_task_ids: set[str] = set()
        blocked_task_ids_this_run: set[str] = set()
        integration_gate_index = 0
        integration_blocked = False
        tolerated_integration_failures: list[IntegrationGateResult] = []
        worker_verification_warnings: dict[str, str] = {}
        final_gate_task_ids: list[str] = []
        final_gate_paths: list[str] = []
        recovered_tasks: dict[str, str] = _recover_stale_in_progress(paths=paths, plan=plan)
        for task_id, status in recovered_tasks.items():
            _trace(
                "swarm.recovery",
                f"Recovered stale in-progress task ({status}).",
                task_id=task_id,
            )

        startup = backend.prepare_startup(paths.root)
        startup_warnings = startup.warnings
        if effective_integration_mode == "off":
            startup_warnings.append(
                "Integration verification is explicitly off; pre-merge candidate acceptance is disabled and swarm may accept red combined repo states."
            )
        for warning in startup_warnings:
            _trace("swarm.startup", warning)

        review_blocked = False
        remote_blocked_any = False
        remote_settings = load_remote_settings_from_env()
        remote_provider = "unknown"
        remote_bootstrap_error: str | None = None
        if remote_settings.enabled and not backend.supports_remote_sync:
            warning = (
                f"Remote sync skipped: backend {backend.name} does not support branch push/PR flow."
            )
            startup_warnings.append(warning)
            _trace("remote.lifecycle", warning)
            remote_settings = type(remote_settings)(
                sync_mode="off",
                remote_name=remote_settings.remote_name,
                create_pr=remote_settings.create_pr,
                provider=remote_settings.provider,
            )
        if remote_settings.enabled:
            _trace(
                "remote.lifecycle",
                f"Remote sync enabled for remote {remote_settings.remote_name}.",
                verbosity="full",
            )
            try:
                remote_url = get_remote_url(paths.root, remote_settings.remote_name)
                remote_provider = resolve_provider(
                    settings_provider=remote_settings.provider,
                    remote_url=remote_url,
                )
                _trace(
                    "remote.lifecycle",
                    f"Remote provider resolved to {remote_provider}.",
                    verbosity="full",
                )
            except Exception as e:  # noqa: BLE001
                remote_bootstrap_error = str(e)
                _trace("remote.error", f"Remote bootstrap failed: {remote_bootstrap_error}")

        def _maybe_run_replanning(
            *,
            batch_index: int,
            merged_task_ids: list[str],
            integration_result: IntegrationGateResult,
            force_suggest: bool = False,
        ) -> ReplanAttemptResult | None:
            if effective_replanning_mode == "off":
                return None
            trigger = build_replanning_trigger(
                paths=paths,
                integration_mode=effective_integration_mode,
                merged_task_ids=merged_task_ids,
            )
            if trigger is None:
                _trace(
                    "replanning.lifecycle",
                    f"Skipped replanning after batch {batch_index}: no open integration trigger.",
                    verbosity="full",
                )
                return None
            return replanning_runner(
                paths=paths,
                plan=plan,
                cfg=cfg,
                api_key_override=api_key_override,
                requested_mode=effective_replanning_mode,
                batch_index=batch_index,
                merged_task_ids=merged_task_ids,
                integration_result=integration_result,
                trigger=trigger,
                allow_apply=not force_suggest,
            )

        def _raise_for_replanned_execution_ready(attempt: ReplanAttemptResult) -> None:
            if not (attempt.applied and attempt.plan_changed):
                return
            try:
                raise_for_execution_ready_plan(
                    plan,
                    retry_failed=retry_failed,
                    retry_changes_requested=retry_changes_requested,
                    retry_merge_conflicts=retry_merge_conflicts,
                    only=only,
                )
            except PlannerFailedError as e:
                _trace("replanning.error", str(e))
                raise

        def _record_replanning_attempt(
            attempt: ReplanAttemptResult,
            *,
            schedule_recomputed: bool = False,
        ) -> ReplanAttemptResult:
            recorded = replace(attempt, schedule_recomputed=schedule_recomputed)
            replanning_results.append(recorded)
            _trace(
                "replanning.lifecycle",
                (
                    f"Replanning {recorded.replan_label}: requested={recorded.requested_mode}, "
                    f"effective={recorded.effective_mode}, proposal={'yes' if recorded.proposal_generated else 'no'}, "
                    f"validation={'passed' if recorded.validation_passed else 'failed'}, "
                    f"applied={'yes' if recorded.applied else 'no'}, "
                    f"changed={'yes' if recorded.plan_changed else 'no'}, "
                    f"recomputed={'yes' if recorded.schedule_recomputed else 'no'}."
                ),
            )
            _raise_for_replanned_execution_ready(recorded)
            return recorded

        def _preserve_task_evidence(
            *,
            task_id: str,
            prepared_workspace: PreparedTaskWorkspace,
            reason: str,
            result: Any | None = None,
        ) -> None:
            """Copy the task's diff and verification log into the run's artifacts.

            Best effort by contract: a capture problem is traced as a warning and never
            stops the cleanup it precedes.
            """
            verify_artifact = getattr(result, "verify_artifact_path", None) if result else None
            verify_path = Path(verify_artifact) if verify_artifact else None
            if verify_path is not None and not verify_path.is_absolute():
                verify_path = paths.root / verify_path
            if verify_path is None or not verify_path.exists():
                verify_path = (
                    paths.execution_verify_dir / f"{safe_task_file_component(task_id)}.txt"
                )
            try:
                evidence = preserve_failed_task_evidence(
                    execution_dir=paths.execution_dir,
                    task_id=task_id,
                    worktree_path=prepared_workspace.worktree_path,
                    base_branch=prepared_workspace.base_branch,
                    branch=prepared_workspace.branch,
                    reason=reason,
                    verification_log_path=verify_path if verify_path.exists() else None,
                    verification_summary=(
                        str(getattr(result, "verify_summary", "") or "") if result else None
                    ),
                    extra={
                        "keep_worktrees": bool(keep_worktrees),
                        "failure_reason": (
                            str(getattr(result, "failure_reason", "") or "") if result else ""
                        ),
                    },
                )
            except Exception as e:  # noqa: BLE001
                _trace(
                    "worktree.error",
                    f"Could not preserve task evidence before cleanup: {e}",
                    task_id=task_id,
                )
                return
            if evidence.errors:
                _trace(
                    "worktree.error",
                    "Task evidence capture was incomplete: " + "; ".join(evidence.errors),
                    task_id=task_id,
                )
            if evidence.directory is not None:
                _trace(
                    "worktree.lifecycle",
                    f"Preserved task evidence in {_repo_rel(paths.root, evidence.directory)} "
                    "before workspace cleanup.",
                    task_id=task_id,
                    verbosity="full",
                )

        def _cleanup_nonmerged_workspace(
            task_id: str,
            prepared_workspace: PreparedTaskWorkspace | None,
            *,
            reason: str = "",
            result: Any | None = None,
        ) -> None:
            if prepared_workspace is None:
                return
            # Evidence FIRST, unconditionally, and before anything can remove the
            # worktree: an uncommitted edit or an uncommitted new file exists nowhere
            # else, so cleanup would otherwise be the only record that it ever did.
            _preserve_task_evidence(
                task_id=task_id,
                prepared_workspace=prepared_workspace,
                reason=reason or "failed task workspace cleanup",
                result=result,
            )
            cleanup_errors = backend.cleanup_failed_task_workspace(
                root=paths.root,
                prepared_workspace=prepared_workspace,
                keep_worktrees=keep_worktrees,
            )
            if cleanup_errors:
                task = find_task(plan, task_id)
                detail = (
                    "failed task cleanup is incomplete; future reruns are blocked until cleanup "
                    f"succeeds: {'; '.join(cleanup_errors)}"
                )
                if task is not None:
                    existing_error = str(task.get("last_error") or "").strip()
                    if existing_error and detail not in existing_error:
                        task["last_error"] = f"{existing_error}; {detail}"
                    else:
                        task["last_error"] = detail
                    save_plan(paths, plan)
                _trace(
                    "worktree.error",
                    f"Failure cleanup blocked future reruns: {'; '.join(cleanup_errors)}",
                    task_id=task_id,
                )
            elif not keep_worktrees:
                _trace(
                    "worktree.lifecycle",
                    "Cleaned failed task workspace and branch state for future reruns.",
                    task_id=task_id,
                    verbosity="full",
                )

        pending_promoted_knowledge = False
        pending_task_attempt_resolutions = False
        pending_ready_items: list[ReadyBatchItem] = []
        if not dry_run and not _cancellation_requested() and merge_strategy == "auto":
            pending_ready: list[dict[str, Any]] = []
            for task in plan.get("tasks") or []:
                status = canonical_task_status(str(task.get("status") or ""))
                if status == "ready_for_merge":
                    if _should_defer_red_regression_ready_task(plan, task):
                        task_id = str(task.get("id") or "")
                        _trace(
                            "merge.lifecycle",
                            (
                                "Deferring expected-red regression task until its dependent "
                                "implementation is ready for the same pre-merge candidate."
                            ),
                            task_id=task_id,
                            verbosity="full",
                        )
                        continue
                    pending_ready.append(task)
                if retry_merge_conflicts and status == "merge_conflict":
                    pending_ready.append(task)

            if pending_ready:
                pending_ids = ", ".join(str(task.get("id") or "") for task in pending_ready)
                _trace(
                    "merge.lifecycle",
                    f"Pending merge queue: {pending_ids}.",
                    verbosity="full",
                )

            for task in pending_ready:
                task_id = str(task.get("id") or "")
                branch = str(task.get("branch") or "")
                if not branch:
                    continue
                observed_task_ids.add(task_id)
                try:
                    prepared_workspace = backend.load_task_workspace(
                        root=paths.root,
                        run_dir=paths.run_dir,
                        task_id=task_id,
                        branch=branch,
                        base_branch=selected_base,
                    )
                except GitOpsError as e:
                    _mark_status(paths, plan, task_id, "failed")
                    execution_skipped[task_id] = str(e)
                    _trace("worktree.error", str(e), task_id=task_id)
                    continue
                remote_record: dict[str, object] | None = None
                if remote_settings.enabled:
                    _trace(
                        "remote.lifecycle", "Syncing remote branch before merge.", task_id=task_id
                    )
                    remote_record = init_remote_record(
                        task_id=task_id,
                        remote=remote_settings.remote_name,
                        provider=remote_provider,
                    )
                    raw_errors = remote_record["errors"]
                    assert isinstance(raw_errors, list)
                    remote_blocked = False
                    if remote_bootstrap_error:
                        msg = f"remote discovery failed: {remote_bootstrap_error}"
                        raw_errors.append(msg)
                        if remote_settings.strict:
                            remote_blocked = True
                    else:
                        pushed_branch, push_output = push_branch(
                            paths.root,
                            remote=remote_settings.remote_name,
                            branch=branch,
                        )
                        remote_record["pushed_branch"] = pushed_branch
                        remote_record["branch_push_output"] = truncate_output(push_output)
                        if not pushed_branch:
                            msg = f"remote branch push failed: {push_output or 'unknown error'}"
                            raw_errors.append(msg)
                            if remote_settings.strict:
                                remote_blocked = True
                        if pushed_branch and remote_settings.create_pr:
                            _trace(
                                "remote.lifecycle",
                                "Creating remote PR/MR.",
                                task_id=task_id,
                                verbosity="full",
                            )
                            created_pr, pr_url, pr_id, pr_output = ensure_pr_or_mr(
                                paths.root,
                                provider=str(remote_record.get("provider") or "unknown"),
                                base_branch=selected_base,
                                head_branch=branch,
                                title=f"{task_id}: {str(task.get('title') or '').strip() or 'task update'}",
                                body=str(task.get("description") or "")[:4000],
                            )
                            remote_record["created_pr"] = created_pr
                            remote_record["pr_url"] = pr_url
                            remote_record["pr_number_or_iid"] = pr_id
                            remote_record["pr_output"] = truncate_output(pr_output)
                            if created_pr and pr_url:
                                task["remote_pr_url"] = pr_url
                                task["remote_provider"] = str(
                                    remote_record.get("provider") or "unknown"
                                )
                                save_plan(paths, plan)
                            if not created_pr:
                                msg = (
                                    f"remote PR/MR creation failed: {pr_output or 'unknown error'}"
                                )
                                raw_errors.append(msg)
                                if remote_settings.strict:
                                    remote_blocked = True
                    write_remote_record(
                        execution_dir=paths.execution_dir,
                        task_id=task_id,
                        record=remote_record,
                    )
                    if remote_blocked:
                        remote_blocked_any = True
                        _mark_status(paths, plan, task_id, "failed")
                        execution_skipped[task_id] = "blocked by strict remote sync failure"
                        pending_task_attempt_resolutions = (
                            _resolve_worker_task_attempt_acceptance(
                                paths,
                                plan=plan,
                                task_id=task_id,
                                acceptance_state="rejected",
                                summary=(
                                    "Worker result was rejected because strict remote sync "
                                    "blocked acceptance."
                                ),
                            )
                            or pending_task_attempt_resolutions
                        )
                        _mark_worker_knowledge_capture_skipped(
                            paths,
                            task_id=task_id,
                            reason="worker result was blocked by strict remote sync before acceptance",
                        )
                        _cleanup_nonmerged_workspace(
                            task_id=task_id,
                            prepared_workspace=prepared_workspace,
                            reason="strict remote sync blocked acceptance",
                        )
                        _trace(
                            "remote.error",
                            "Blocked by strict remote sync failure.",
                            task_id=task_id,
                        )
                        _append_remote_report_update(
                            paths=paths,
                            report_path_raw=os.fspath(
                                paths.execution_reports_dir / f"{task_id}.md"
                            ),
                            record=remote_record,
                        )
                        continue

                if review:
                    _trace("review.lifecycle", "Running review gate.", task_id=task_id)
                    try:
                        review_outcome = review_runner(
                            paths=paths,
                            plan=plan,
                            task=task,
                            cfg=cfg,
                            api_key_override=api_key_override,
                        )
                    except ReviewError as e:
                        _mark_status(paths, plan, task_id, "failed")
                        execution_skipped[task_id] = f"review failed: {e}"
                        review_blocked = True
                        pending_task_attempt_resolutions = (
                            _resolve_worker_task_attempt_acceptance(
                                paths,
                                plan=plan,
                                task_id=task_id,
                                acceptance_state="rejected",
                                summary=f"Worker result was rejected because review failed: {e}",
                            )
                            or pending_task_attempt_resolutions
                        )
                        _mark_worker_knowledge_capture_skipped(
                            paths,
                            task_id=task_id,
                            reason="worker result was rejected before acceptance because review failed",
                        )
                        _cleanup_nonmerged_workspace(
                            task_id=task_id,
                            prepared_workspace=prepared_workspace,
                            reason=f"review failed: {e}",
                        )
                        _trace("review.error", f"Review failed: {e}", task_id=task_id)
                        continue
                    if not review_outcome.approved:
                        _mark_status(paths, plan, task_id, "changes_requested")
                        execution_skipped[task_id] = "changes requested by review gate"
                        review_blocked = True
                        pending_task_attempt_resolutions = (
                            _resolve_worker_task_attempt_acceptance(
                                paths,
                                plan=plan,
                                task_id=task_id,
                                acceptance_state="rejected",
                                summary="Worker result was rejected because review requested changes.",
                            )
                            or pending_task_attempt_resolutions
                        )
                        _mark_worker_knowledge_capture_skipped(
                            paths,
                            task_id=task_id,
                            reason="worker result was not accepted because review requested changes",
                        )
                        _cleanup_nonmerged_workspace(
                            task_id=task_id,
                            prepared_workspace=prepared_workspace,
                            reason="changes requested by review gate",
                        )
                        _trace("review.error", "Changes requested by review gate.", task_id=task_id)
                        continue
                    _trace(
                        "review.lifecycle", "Review approved.", task_id=task_id, verbosity="full"
                    )

                if not backend.branch_exists(prepared_workspace.control_root, branch):
                    _mark_status(paths, plan, task_id, "failed")
                    pending_task_attempt_resolutions = (
                        _resolve_worker_task_attempt_acceptance(
                            paths,
                            plan=plan,
                            task_id=task_id,
                            acceptance_state="rejected",
                            summary="Worker result was rejected because the task branch was missing.",
                        )
                        or pending_task_attempt_resolutions
                    )
                    _mark_worker_knowledge_capture_skipped(
                        paths,
                        task_id=task_id,
                        reason="worker result could not be accepted because the task branch is missing",
                    )
                    _cleanup_nonmerged_workspace(
                        task_id=task_id,
                        prepared_workspace=prepared_workspace,
                        reason="task branch does not exist",
                    )
                    outcome = MergeOutcome(
                        task_id=task_id,
                        branch=branch,
                        success=False,
                        merge_commit_hash=None,
                        error="branch does not exist",
                        backend_name=backend.name,
                        action="applied" if backend.name == "snapshot_workspace" else "merged",
                    )
                    _write_merge_result(paths, outcome)
                    merge_outcomes.append(outcome)
                    _trace("merge.error", "Merge branch does not exist.", task_id=task_id)
                    continue

                pending_ready_items.append(
                    ReadyBatchItem(
                        task=task,
                        prepared_workspace=prepared_workspace,
                        changed_files=tuple(_load_worker_changed_files(paths, task_id)),
                        report_path_raw=os.fspath(paths.execution_reports_dir / f"{task_id}.md"),
                        remote_record=remote_record,
                    )
                )

        if pending_ready_items:
            integration_gate_index += 1
            pending_task_ids = [item.task_id for item in pending_ready_items]
            startup_integration_result, startup_rejection_reason = _verify_ready_batch_candidate(
                paths=paths,
                plan=plan,
                cfg=cfg,
                workspace_scan=workspace_scan,
                backend=backend,
                batch_index=integration_gate_index,
                base_branch=selected_base,
                integration_mode=effective_integration_mode,
                allow_transient_dependent_integration_warning=(effective_replanning_mode == "off"),
                integration_verify_cmd=integration_verify_cmd,
                verify_cmd=verify_cmd,
                keep_worktrees=keep_worktrees,
                ready_items=pending_ready_items,
                trace=lambda phase, message: _trace(phase, message),
                task_trace=lambda task_id, message: _trace(
                    "integration.error", message, task_id=task_id
                ),
                integration_runner=integration_runner,
            )
            if startup_integration_result is not None:
                integration_results.append(startup_integration_result)
            if (
                startup_integration_result is not None
                and not startup_integration_result.passed
                and startup_rejection_reason is None
            ):
                tolerated_integration_failures.append(startup_integration_result)
                record_integration_failure_knowledge(
                    paths=paths,
                    result=startup_integration_result,
                )
            if startup_rejection_reason is not None:
                if startup_integration_result is not None:
                    record_integration_failure_knowledge(
                        paths=paths,
                        result=startup_integration_result,
                    )
                pending_task_attempt_resolutions = (
                    _reject_ready_batch_items(
                        paths=paths,
                        plan=plan,
                        backend=backend,
                        keep_worktrees=keep_worktrees,
                        ready_items=pending_ready_items,
                        reason=startup_rejection_reason,
                        merge_outcomes=merge_outcomes,
                        execution_skipped=execution_skipped,
                        cleanup_nonmerged_workspace=_cleanup_nonmerged_workspace,
                    )
                    or pending_task_attempt_resolutions
                )
                if pending_task_attempt_resolutions:
                    rebuild_knowledge_index(paths)
                if startup_integration_result is not None:
                    startup_replan_attempt = _maybe_run_replanning(
                        batch_index=integration_gate_index,
                        merged_task_ids=pending_task_ids,
                        integration_result=startup_integration_result,
                        force_suggest=effective_integration_mode == "strict",
                    )
                    if startup_replan_attempt is not None:
                        _record_replanning_attempt(startup_replan_attempt)
                if effective_integration_mode == "strict":
                    integration_blocked = True
                    reason = (
                        f"blocked by strict integration gate batch_{integration_gate_index:03d}: "
                        f"{startup_rejection_reason}"
                    )
                    blocked_task_ids = _mark_remaining_tasks_blocked_by_integration(
                        paths=paths,
                        plan=plan,
                        reason=reason,
                        retry_failed=retry_failed,
                        retry_changes_requested=retry_changes_requested,
                        parallel=parallel,
                        max_attempts=effective_max_attempts,
                        only_ids=only_ids,
                    )
                    for task_id in blocked_task_ids:
                        execution_skipped[task_id] = reason
                        _trace("integration.error", reason, task_id=task_id)
            else:
                (
                    pending_merged_task_ids,
                    pending_remote_task_ids,
                    pending_merged_paths,
                    pending_promoted_knowledge,
                    batch_task_attempt_resolutions,
                ) = _merge_ready_batch_items_into_base(
                    paths=paths,
                    plan=plan,
                    cfg=cfg,
                    api_key_override=api_key_override,
                    backend=backend,
                    base_branch=selected_base,
                    keep_worktrees=keep_worktrees,
                    verify_mode=verify_mode,
                    verify_commands=_get_verify_commands(),
                    verify_command_selection=(
                        _get_verify_command_selection() if verify_mode != "off" else None
                    ),
                    ready_items=pending_ready_items,
                    merge_outcomes=merge_outcomes,
                    trace=_trace,
                    cleanup_nonmerged_workspace=_cleanup_nonmerged_workspace,
                )
                pending_task_attempt_resolutions = (
                    batch_task_attempt_resolutions or pending_task_attempt_resolutions
                )
                final_gate_task_ids.extend(pending_merged_task_ids)
                final_gate_paths.extend(pending_merged_paths)
                if remote_settings.enabled and pending_remote_task_ids:
                    _trace(
                        "remote.lifecycle",
                        f"Pushing updated base branch {selected_base}.",
                        verbosity="full",
                    )
                    pushed_base, base_output = push_base(
                        paths.root,
                        remote=remote_settings.remote_name,
                        base_branch=selected_base,
                    )
                    for task_id in pending_remote_task_ids:
                        record_path = paths.execution_dir / "remote" / f"{task_id}.json"
                        if not record_path.exists():
                            continue
                        payload = json.loads(record_path.read_text(encoding="utf-8"))
                        payload["pushed_base"] = pushed_base
                        payload["base_push_output"] = truncate_output(base_output)
                        if not pushed_base:
                            errors = payload.setdefault("errors", [])
                            if isinstance(errors, list):
                                errors.append(
                                    f"remote base push failed: {base_output or 'unknown error'}"
                                )
                                _trace(
                                    "remote.error",
                                    f"Base push failed: {base_output or 'unknown error'}",
                                    task_id=task_id,
                                )
                        write_remote_record(
                            execution_dir=paths.execution_dir,
                            task_id=task_id,
                            record=payload,
                        )
                        _append_remote_report_update(
                            paths=paths,
                            report_path_raw=os.fspath(
                                paths.execution_reports_dir / f"{task_id}.md"
                            ),
                            record=payload,
                        )
                if pending_promoted_knowledge or pending_task_attempt_resolutions:
                    rebuild_knowledge_index(paths)
                if (
                    startup_integration_result is not None
                    and startup_integration_result.passed
                    and len(pending_merged_task_ids) == len(pending_task_ids)
                ):
                    record_integration_resolution_knowledge(
                        paths=paths,
                        result=startup_integration_result,
                    )
                    startup_replan_attempt = _maybe_run_replanning(
                        batch_index=integration_gate_index,
                        merged_task_ids=pending_task_ids,
                        integration_result=startup_integration_result,
                        force_suggest=False,
                    )
                    if startup_replan_attempt is not None:
                        _record_replanning_attempt(startup_replan_attempt)

        while True:
            if integration_blocked:
                break
            remaining = (
                max_tasks - len(executed) if (max_tasks is not None and max_tasks > 0) else None
            )
            if remaining is not None and remaining <= 0:
                break
            schedule = compute_schedule(
                base_branch=selected_base,
                tasks=plan.get("tasks") or [],
                parallel=parallel,
                max_tasks=remaining,
                retry_failed=retry_failed,
                retry_changes_requested=retry_changes_requested,
                max_attempts=effective_max_attempts,
                only_ids=only_ids,
            )
            runnable_candidates = [
                candidate
                for candidate in schedule.runnable
                if candidate.task_id not in blocked_task_ids_this_run
            ]
            for task_id, reason in schedule.skipped.items():
                if task_id in observed_task_ids:
                    continue
                scheduler_skipped[task_id] = reason
            schedule_preview = [
                [task_id for task_id in batch.task_ids if task_id not in blocked_task_ids_this_run]
                for batch in schedule.batches
                if any(task_id not in blocked_task_ids_this_run for task_id in batch.task_ids)
            ]
            _trace(
                "scheduler.lifecycle",
                (
                    f"Schedule resolved: {len(runnable_candidates)} runnable task(s) "
                    f"in {len(schedule_preview)} batch(es)."
                ),
            )
            schedule_recompute_requested = False

            for batch in schedule.batches:
                filtered_task_ids = [
                    task_id
                    for task_id in batch.task_ids
                    if task_id not in blocked_task_ids_this_run
                ]
                if not filtered_task_ids:
                    continue
                _trace(
                    "scheduler.batch",
                    f"Batch {batch.index}: {', '.join(filtered_task_ids)}.",
                )
                for reason in batch.reasons:
                    _trace("scheduler.batch", reason, verbosity="full")
            for task_id, reason in schedule.skipped.items():
                _trace(
                    "scheduler.skip",
                    f"Skipped by scheduler: {reason}",
                    task_id=task_id,
                    verbosity="full",
                )

            if dry_run:
                console.rule("[bold cyan]forge swarm (dry-run)[/bold cyan]")
                console.print(f"Base branch: {selected_base}")
                for line in workspace_summary_lines:
                    console.print(line)
                runnable_ids = ", ".join(c.task_id for c in runnable_candidates) or "(none)"
                console.print(f"Runnable tasks: {runnable_ids}")
                if schedule.ready_for_merge:
                    pending = ", ".join(t.task_id for t in schedule.ready_for_merge)
                    console.print(f"Ready for merge: {pending}")
                for batch in schedule.batches:
                    filtered_task_ids = [
                        task_id
                        for task_id in batch.task_ids
                        if task_id not in blocked_task_ids_this_run
                    ]
                    if not filtered_task_ids:
                        continue
                    console.print(f"Batch {batch.index}: {', '.join(filtered_task_ids)}")
                    for reason in batch.reasons:
                        console.print(f"  - {reason}")
                summary_path = _write_swarm_summary(
                    paths=paths,
                    backend_name=backend.name,
                    base_branch=selected_base,
                    executed=executed,
                    merge_outcomes=merge_outcomes,
                    integration_results=integration_results,
                    replanning_results=[],
                    skipped=schedule.skipped,
                    recovered=recovered_tasks,
                    startup_warnings=startup_warnings,
                    dry_run=True,
                    schedule_preview=schedule_preview,
                    workspace_summary_lines=workspace_summary_lines,
                    binding_summary_lines=binding_summary_lines,
                )
                _trace(
                    "swarm.lifecycle",
                    f"Dry-run complete. Summary: {summary_path}.",
                )
                return 0

            if not runnable_candidates:
                break

            for batch in schedule.batches:
                filtered_task_ids = [
                    task_id
                    for task_id in batch.task_ids
                    if task_id not in blocked_task_ids_this_run
                ]
                if not filtered_task_ids:
                    continue
                _trace(
                    "scheduler.batch",
                    f"Starting batch {batch.index}: {', '.join(filtered_task_ids)}.",
                )
                executed_batch_history.append(filtered_task_ids[:])
                observed_task_ids.update(filtered_task_ids)
                batch_tasks: list[dict[str, Any]] = []
                prepared_workspaces: dict[str, PreparedTaskWorkspace] = {}
                for task_id in filtered_task_ids:
                    task = find_task(plan, task_id)
                    if task is None:
                        continue
                    branch = str(task.get("branch") or "").strip()
                    if not branch:
                        title = str(task.get("title") or "")
                        from .git_ops import generate_task_branch_name

                        branch = generate_task_branch_name(task_id, title)
                        task["branch"] = branch
                        save_plan(paths, plan)

                    _trace(
                        "worktree.lifecycle",
                        f"Preparing worktree on branch {branch}.",
                        task_id=task_id,
                        verbosity="full",
                    )
                    try:
                        prepared_workspace = backend.prepare_task_workspace(
                            root=paths.root,
                            run_dir=paths.run_dir,
                            task_id=task_id,
                            branch=branch,
                            base_branch=selected_base,
                        )
                    except Exception as e:  # noqa: BLE001
                        error = f"worktree setup failed: {e}"
                        task["last_error"] = error
                        _mark_status(paths, plan, task_id, "failed")
                        blocked_task_ids_this_run.add(task_id)
                        execution_skipped[task_id] = error
                        _trace("worktree.error", error, task_id=task_id)
                        continue
                    prepared_workspaces[task_id] = prepared_workspace
                    batch_tasks.append(task)
                    _trace(
                        "worktree.lifecycle",
                        f"Worktree ready on branch {branch}.",
                        task_id=task_id,
                        verbosity="full",
                    )

                if not batch_tasks:
                    _trace(
                        "scheduler.batch",
                        f"Batch {batch.index} ended with no runnable tasks.",
                        verbosity="full",
                    )
                    continue

                for task in batch_tasks:
                    task_id = str(task.get("id") or "")
                    _bump_attempt(paths, plan, task_id)
                    _mark_status(paths, plan, task_id, "in_progress")
                    attempts = int(task.get("attempts") or 0)
                    _trace(
                        "worker.lifecycle",
                        f"Worker started (attempt {attempts}).",
                        task_id=task_id,
                    )

                worker_results: list[TaskWorkerResult] = []
                max_workers = min(max(1, parallel), len(batch_tasks))
                worker_verify_contracts = {
                    str(task.get("id") or ""): resolve_worker_verify_contract(
                        cfg=cfg,
                        verify_mode=verify_mode,
                        verify_commands=(
                            (_get_verify_commands() or None) if verify_mode != "off" else None
                        ),
                        verify_command_selection=(
                            _get_verify_command_selection() if verify_mode != "off" else None
                        ),
                        task=task,
                        root=prepared_workspaces[str(task.get("id") or "")].worktree_path,
                        repo_scan=workspace_scan,
                        plan_requirements=plan_requirements,
                    )
                    for task in batch_tasks
                }
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    future_map = {
                        pool.submit(
                            worker_runner,
                            task=task,
                            plan=plan,
                            worktree_repo_path=prepared_workspaces[
                                str(task.get("id") or "")
                            ].worktree_path,
                            base_branch=selected_base,
                            run_paths=paths,
                            cfg=worker_verify_contracts[str(task.get("id") or "")].cfg,
                            mode=mode,
                            yes=yes,
                            max_steps=max_steps,
                            api_key_override=api_key_override,
                            no_log=no_log,
                            console=make_console(
                                file=io.StringIO(), force_terminal=False, no_color=True
                            ),
                            scope_mode=scope_mode,
                            verify_mode=verify_mode,
                            verify_commands=list(
                                worker_verify_contracts[str(task.get("id") or "")].commands
                            ),
                            verify_command_selection=worker_verify_contracts[
                                str(task.get("id") or "")
                            ].selection,
                            trace_sink=sink,
                            trace_level=trace_level_normalized,
                            **(
                                {"cancellation_event": cancellation_event}
                                if cancellation_event is not None
                                else {}
                            ),
                            **(
                                {"approval_handler": worker_approval_handler}
                                if worker_approval_handler is not None
                                else {}
                            ),
                        ): str(task.get("id") or "")
                        for task in batch_tasks
                    }
                    for fut in as_completed(future_map):
                        task_id = future_map[fut]
                        try:
                            result = reject_abnormal_success_result(fut.result())
                        except Exception as e:  # noqa: BLE001
                            _trace("worker.error", f"Worker crashed: {e}", task_id=task_id)
                            raise
                        worker_results.append(result)
                        if result.success:
                            success_message = "Worker finished successfully."
                            if result.noop_success:
                                success_message = (
                                    "Worker finished successfully; task was already satisfied."
                                )
                            _trace(
                                "worker.lifecycle",
                                success_message,
                                task_id=result.task_id,
                            )
                        else:
                            _trace(
                                "worker.error",
                                f"Worker finished with failure: {_truncate_inline(result.error or result.summary, max_chars=180)}",
                                task_id=result.task_id,
                            )

                batch_merged_task_ids: list[str] = []
                batch_remote_task_ids: list[str] = []
                _batch_merged_paths: list[str] = []
                batch_promoted_knowledge = False
                batch_task_attempt_resolutions = False
                for result_index, result in enumerate(worker_results):
                    _persist_worker_result(paths, result)
                    executed.append(result.task_id)
                    if result.success:
                        nonexecuting_verify_reason = (
                            _worker_result_nonexecuting_verification_reason(result)
                        )
                        if nonexecuting_verify_reason is not None:
                            reason = (
                                f"verification did not execute tests: {nonexecuting_verify_reason}"
                            )
                            if verify_mode == "warn":
                                worker_verification_warnings[result.task_id] = reason
                                result = replace(
                                    result,
                                    warnings=[
                                        *result.warnings,
                                        f"Verification warning: {reason}",
                                    ],
                                )
                                worker_results[result_index] = result
                                _trace("verify.warning", reason, task_id=result.task_id)
                            else:
                                _mark_status(paths, plan, result.task_id, "verify_failed")
                                execution_skipped[result.task_id] = reason
                                batch_task_attempt_resolutions = (
                                    _resolve_worker_task_attempt_acceptance(
                                        paths,
                                        plan=plan,
                                        task_id=result.task_id,
                                        acceptance_state="rejected",
                                        summary=(f"Worker result was rejected because {reason}."),
                                        result=result,
                                    )
                                    or batch_task_attempt_resolutions
                                )
                                _mark_worker_knowledge_capture_skipped(
                                    paths,
                                    task_id=result.task_id,
                                    result=result,
                                    reason="worker result was not accepted because verification did not execute tests",
                                )
                                _cleanup_nonmerged_workspace(
                                    task_id=result.task_id,
                                    prepared_workspace=prepared_workspaces.get(result.task_id),
                                    reason=execution_skipped.get(result.task_id, ""),
                                    result=result,
                                )
                                _trace("verify.error", reason, task_id=result.task_id)
                                continue
                        if result.noop_success:
                            task = find_task(plan, result.task_id)
                            if task is None:
                                _mark_status(paths, plan, result.task_id, "failed")
                                execution_skipped[result.task_id] = (
                                    "acceptance failed: task missing"
                                )
                                batch_task_attempt_resolutions = (
                                    _resolve_worker_task_attempt_acceptance(
                                        paths,
                                        plan=plan,
                                        task_id=result.task_id,
                                        acceptance_state="rejected",
                                        summary=(
                                            "Worker result was rejected because the task was missing "
                                            "before already-satisfied acceptance."
                                        ),
                                        result=result,
                                    )
                                    or batch_task_attempt_resolutions
                                )
                                _mark_worker_knowledge_capture_skipped(
                                    paths,
                                    task_id=result.task_id,
                                    result=result,
                                    reason="worker result could not be accepted because the task is missing",
                                )
                                _cleanup_nonmerged_workspace(
                                    task_id=result.task_id,
                                    prepared_workspace=prepared_workspaces.get(result.task_id),
                                    reason=execution_skipped.get(result.task_id, ""),
                                    result=result,
                                )
                                _trace(
                                    "worker.error",
                                    "Already-satisfied acceptance failed: task missing.",
                                    task_id=result.task_id,
                                )
                                continue
                            outcome = _accept_noop_task(
                                paths=paths,
                                plan=plan,
                                task=task,
                                backend=backend,
                                keep_worktrees=keep_worktrees,
                                prepared_workspace=prepared_workspaces[result.task_id],
                                result=result,
                            )
                            merge_outcomes.append(outcome)
                            batch_task_attempt_resolutions = (
                                _resolve_worker_task_attempt_acceptance(
                                    paths,
                                    plan=plan,
                                    task_id=result.task_id,
                                    acceptance_state="accepted",
                                    result=result,
                                    summary=(
                                        "Worker result was accepted as an already-satisfied no-op."
                                    ),
                                )
                                or batch_task_attempt_resolutions
                            )
                            promotion_result = _promote_worker_knowledge_capture(
                                paths,
                                plan=plan,
                                task_id=result.task_id,
                                result=result,
                            )
                            if promotion_result is not None and (
                                promotion_result.fact_entry_ids
                                or promotion_result.decision_entry_ids
                            ):
                                batch_promoted_knowledge = True
                            message = _merge_outcome_success_trace_message(outcome)
                            if outcome.cleanup_error:
                                message += f" Cleanup warning: {outcome.cleanup_error}"
                            _trace("worker.lifecycle", message, task_id=result.task_id)
                            continue
                        if review:
                            task = find_task(plan, result.task_id)
                            if task is None:
                                _mark_status(paths, plan, result.task_id, "failed")
                                execution_skipped[result.task_id] = "review failed: task missing"
                                review_blocked = True
                                _resolve_worker_task_attempt_acceptance(
                                    paths=paths,
                                    plan=plan,
                                    task_id=result.task_id,
                                    acceptance_state="rejected",
                                    summary=(
                                        "Worker result was rejected because the task was missing "
                                        "before review."
                                    ),
                                    result=result,
                                )
                                _mark_worker_knowledge_capture_skipped(
                                    paths,
                                    task_id=result.task_id,
                                    result=result,
                                    reason="worker result could not be accepted because the task is missing",
                                )
                                _cleanup_nonmerged_workspace(
                                    task_id=result.task_id,
                                    prepared_workspace=prepared_workspaces.get(result.task_id),
                                    reason=execution_skipped.get(result.task_id, ""),
                                    result=result,
                                )
                                _trace(
                                    "review.error",
                                    "Review failed: task missing.",
                                    task_id=result.task_id,
                                )
                                continue
                            _trace(
                                "review.lifecycle", "Running review gate.", task_id=result.task_id
                            )
                            try:
                                review_outcome = review_runner(
                                    paths=paths,
                                    plan=plan,
                                    task=task,
                                    cfg=cfg,
                                    api_key_override=api_key_override,
                                    verification_payload_override=result.verify_payload,
                                )
                            except ReviewError as e:
                                _mark_status(paths, plan, result.task_id, "failed")
                                execution_skipped[result.task_id] = f"review failed: {e}"
                                review_blocked = True
                                _resolve_worker_task_attempt_acceptance(
                                    paths,
                                    plan=plan,
                                    task_id=result.task_id,
                                    acceptance_state="rejected",
                                    summary=f"Worker result was rejected because review failed: {e}",
                                    result=result,
                                )
                                _mark_worker_knowledge_capture_skipped(
                                    paths,
                                    task_id=result.task_id,
                                    result=result,
                                    reason="worker result was rejected before acceptance because review failed",
                                )
                                _cleanup_nonmerged_workspace(
                                    task_id=result.task_id,
                                    prepared_workspace=prepared_workspaces.get(result.task_id),
                                    reason=execution_skipped.get(result.task_id, ""),
                                    result=result,
                                )
                                _trace(
                                    "review.error", f"Review failed: {e}", task_id=result.task_id
                                )
                                continue
                            if not review_outcome.approved:
                                _mark_status(paths, plan, result.task_id, "changes_requested")
                                execution_skipped[result.task_id] = (
                                    "changes requested by review gate"
                                )
                                review_blocked = True
                                _resolve_worker_task_attempt_acceptance(
                                    paths,
                                    plan=plan,
                                    task_id=result.task_id,
                                    acceptance_state="rejected",
                                    summary="Worker result was rejected because review requested changes.",
                                    result=result,
                                )
                                _mark_worker_knowledge_capture_skipped(
                                    paths,
                                    task_id=result.task_id,
                                    result=result,
                                    reason="worker result was not accepted because review requested changes",
                                )
                                _cleanup_nonmerged_workspace(
                                    task_id=result.task_id,
                                    prepared_workspace=prepared_workspaces.get(result.task_id),
                                    reason=execution_skipped.get(result.task_id, ""),
                                    result=result,
                                )
                                _trace(
                                    "review.error",
                                    "Changes requested by review gate.",
                                    task_id=result.task_id,
                                )
                                continue
                            _trace(
                                "review.lifecycle",
                                "Review approved.",
                                task_id=result.task_id,
                                verbosity="full",
                            )
                        if result.verification_unavailable:
                            # Nothing merges without a check behind it, and nothing is
                            # cleaned up either: the commit, its branch and its worktree
                            # stay exactly where they are for a human to review.
                            _mark_status(
                                paths,
                                plan,
                                result.task_id,
                                TASK_STATUS_COMPLETED_UNVERIFIED,
                            )
                            batch_task_attempt_resolutions = (
                                _resolve_worker_task_attempt_acceptance(
                                    paths,
                                    plan=plan,
                                    task_id=result.task_id,
                                    acceptance_state="accepted",
                                    summary=(
                                        "Worker result was kept as an unverified completion: "
                                        "no authoritative verification command exists for this "
                                        "workspace, so nothing was merged."
                                    ),
                                    result=result,
                                )
                                or batch_task_attempt_resolutions
                            )
                            _mark_worker_knowledge_capture_skipped(
                                paths,
                                task_id=result.task_id,
                                result=result,
                                reason=(
                                    "worker result completed without any authoritative verification"
                                ),
                            )
                            _trace(
                                "verify.warning",
                                (
                                    "No authoritative verification command exists; task kept "
                                    "as completed_unverified and left unmerged for review."
                                ),
                                task_id=result.task_id,
                            )
                            continue
                        _mark_status(paths, plan, result.task_id, "ready_for_merge")
                    else:
                        if result.interrupted:
                            interrupted_tasks.add(result.task_id)
                            _mark_status(paths, plan, result.task_id, "interrupted")
                            execution_skipped[result.task_id] = (
                                result.error or "interrupted by cooperative cancellation"
                            )
                            _trace(
                                "worker.lifecycle",
                                "Worker interrupted; preserving task workspace for review.",
                                task_id=result.task_id,
                            )
                            continue
                        noop_verification_failed = (
                            result.failure_reason == "noop_verification_failed"
                        )
                        verification_unavailable = (
                            result.failure_reason == "verification_unavailable"
                        )
                        verification_infra_unavailable = (
                            result.failure_reason == "verification_infra_unavailable"
                            or failure_category_value(result.failure_category)
                            == FailureCategory.INFRA_UNAVAILABLE.value
                        )
                        _mark_status(
                            paths,
                            plan,
                            result.task_id,
                            "verify_failed" if result.verify_failed else "failed",
                        )
                        _mark_worker_knowledge_capture_skipped(
                            paths,
                            task_id=result.task_id,
                            result=result,
                            reason="worker execution outcome was not accepted",
                        )
                        if result.verify_failed:
                            if verification_infra_unavailable:
                                execution_skipped[result.task_id] = (
                                    "verification infrastructure unavailable: "
                                    f"{result.verify_summary or result.summary}"
                                )
                            else:
                                execution_skipped[result.task_id] = (
                                    "strict verification failed: "
                                    f"{result.verify_summary or result.summary}"
                                )
                        elif noop_verification_failed:
                            execution_skipped[result.task_id] = (
                                "already-satisfied verification failed: "
                                f"{result.verify_summary or result.summary}"
                            )
                        elif verification_infra_unavailable:
                            execution_skipped[result.task_id] = (
                                "verification infrastructure unavailable: "
                                f"{result.verify_summary or result.summary}"
                            )
                        elif verification_unavailable:
                            execution_skipped[result.task_id] = (
                                "strict verification could not run: "
                                f"{result.verify_summary or result.summary}"
                            )
                        else:
                            execution_skipped[result.task_id] = (
                                f"worker failed: {result.error or result.summary}"
                            )
                        _cleanup_nonmerged_workspace(
                            task_id=result.task_id,
                            prepared_workspace=prepared_workspaces.get(result.task_id),
                            reason=execution_skipped.get(result.task_id, ""),
                            result=result,
                        )
                        if (
                            result.verify_failed
                            or noop_verification_failed
                            or verification_unavailable
                            or verification_infra_unavailable
                        ):
                            _trace(
                                "verify.error",
                                (
                                    "Verification infrastructure unavailable: "
                                    if verification_infra_unavailable
                                    else (
                                        "Strict verification failed: "
                                        if result.verify_failed
                                        else (
                                            "Already-satisfied verification failed: "
                                            if noop_verification_failed
                                            else "Strict verification could not run: "
                                        )
                                    )
                                )
                                + f"{result.verify_summary or result.summary}",
                                task_id=result.task_id,
                            )
                        else:
                            _trace(
                                "worker.error",
                                execution_skipped[result.task_id],
                                task_id=result.task_id,
                            )

                ready_for_merge_results: list[TaskWorkerResult] = []
                worker_result_by_task_id = {result.task_id: result for result in worker_results}
                for task in batch_tasks:
                    task_id = str(task.get("id") or "")
                    result = worker_result_by_task_id.get(task_id)
                    if result is None:
                        continue
                    current_task = find_task(plan, task_id)
                    if current_task is None:
                        continue
                    if (
                        canonical_task_status(str(current_task.get("status") or ""))
                        == "ready_for_merge"
                    ):
                        ready_for_merge_results.append(result)

                ready_items: list[ReadyBatchItem] = []
                for result in ready_for_merge_results:
                    task = find_task(plan, result.task_id)
                    if task is None:
                        continue
                    remote_record: dict[str, object] | None = None
                    if remote_settings.enabled:
                        _trace(
                            "remote.lifecycle",
                            "Syncing remote branch before merge.",
                            task_id=result.task_id,
                        )
                        remote_record = init_remote_record(
                            task_id=result.task_id,
                            remote=remote_settings.remote_name,
                            provider=remote_provider,
                        )
                        raw_errors = remote_record["errors"]
                        assert isinstance(raw_errors, list)
                        remote_blocked = False
                        branch = str(task.get("branch") or "")
                        if remote_bootstrap_error:
                            msg = f"remote discovery failed: {remote_bootstrap_error}"
                            raw_errors.append(msg)
                            if remote_settings.strict:
                                remote_blocked = True
                        else:
                            pushed_branch, push_output = push_branch(
                                paths.root,
                                remote=remote_settings.remote_name,
                                branch=branch,
                            )
                            remote_record["pushed_branch"] = pushed_branch
                            remote_record["branch_push_output"] = truncate_output(push_output)
                            if not pushed_branch:
                                msg = f"remote branch push failed: {push_output or 'unknown error'}"
                                raw_errors.append(msg)
                                if remote_settings.strict:
                                    remote_blocked = True
                            if pushed_branch and remote_settings.create_pr:
                                _trace(
                                    "remote.lifecycle",
                                    "Creating remote PR/MR.",
                                    task_id=result.task_id,
                                    verbosity="full",
                                )
                                created_pr, pr_url, pr_id, pr_output = ensure_pr_or_mr(
                                    paths.root,
                                    provider=str(remote_record.get("provider") or "unknown"),
                                    base_branch=selected_base,
                                    head_branch=branch,
                                    title=(
                                        f"{result.task_id}: "
                                        f"{str(task.get('title') or '').strip() or 'task update'}"
                                    ),
                                    body=str(task.get("description") or "")[:4000],
                                )
                                remote_record["created_pr"] = created_pr
                                remote_record["pr_url"] = pr_url
                                remote_record["pr_number_or_iid"] = pr_id
                                remote_record["pr_output"] = truncate_output(pr_output)
                                if created_pr and pr_url:
                                    task["remote_pr_url"] = pr_url
                                    task["remote_provider"] = str(
                                        remote_record.get("provider") or "unknown"
                                    )
                                    save_plan(paths, plan)
                                if not created_pr:
                                    msg = f"remote PR/MR creation failed: {pr_output or 'unknown error'}"
                                    raw_errors.append(msg)
                                    if remote_settings.strict:
                                        remote_blocked = True

                        write_remote_record(
                            execution_dir=paths.execution_dir,
                            task_id=result.task_id,
                            record=remote_record,
                        )
                        if remote_blocked:
                            remote_blocked_any = True
                            _mark_status(paths, plan, result.task_id, "failed")
                            execution_skipped[result.task_id] = (
                                "blocked by strict remote sync failure"
                            )
                            batch_task_attempt_resolutions = (
                                _resolve_worker_task_attempt_acceptance(
                                    paths,
                                    plan=plan,
                                    task_id=result.task_id,
                                    acceptance_state="rejected",
                                    summary=(
                                        "Worker result was rejected because strict remote sync "
                                        "blocked acceptance."
                                    ),
                                    result=result,
                                )
                                or batch_task_attempt_resolutions
                            )
                            _mark_worker_knowledge_capture_skipped(
                                paths,
                                task_id=result.task_id,
                                result=result,
                                reason="worker result was blocked by strict remote sync before acceptance",
                            )
                            _cleanup_nonmerged_workspace(
                                task_id=result.task_id,
                                prepared_workspace=prepared_workspaces.get(result.task_id),
                                reason=execution_skipped.get(result.task_id, ""),
                                result=result,
                            )
                            _trace(
                                "remote.error",
                                "Blocked by strict remote sync failure.",
                                task_id=result.task_id,
                            )
                            _append_remote_report_update(
                                paths=paths,
                                report_path_raw=result.report_path,
                                record=remote_record,
                            )
                            continue

                    ready_items.append(
                        ReadyBatchItem(
                            task=task,
                            prepared_workspace=prepared_workspaces[result.task_id],
                            changed_files=tuple(result.changed_files),
                            result=result,
                            report_path_raw=result.report_path,
                            remote_record=remote_record,
                        )
                    )

                if merge_strategy == "review":
                    reviewable = [item.task_id for item in ready_items]
                    if reviewable:
                        _trace(
                            "merge.lifecycle",
                            "Review merge strategy: leaving task(s) ready_for_merge "
                            f"for IDE review: {', '.join(reviewable)}.",
                        )
                    if _cancellation_requested():
                        interrupted_tasks.update(reviewable)
                    continue

                deferred_current_items = [
                    item
                    for item in ready_items
                    if _should_defer_red_regression_ready_task(plan, item.task)
                ]
                if deferred_current_items:
                    for item in deferred_current_items:
                        _trace(
                            "merge.lifecycle",
                            (
                                "Deferring expected-red regression task until its dependent "
                                "implementation is ready for the same pre-merge candidate."
                            ),
                            task_id=item.task_id,
                            verbosity="full",
                        )
                    deferred_ids = {item.task_id for item in deferred_current_items}
                    ready_items = [item for item in ready_items if item.task_id not in deferred_ids]

                if ready_items:
                    deferred_dependency_items: list[ReadyBatchItem] = []
                    deferred_dependency_error: str | None = None
                    for dependency_task in _deferred_red_dependency_tasks_for_ready_items(
                        plan=plan,
                        ready_items=ready_items,
                    ):
                        dependency_task_id = str(dependency_task.get("id") or "")
                        dependency_branch = str(dependency_task.get("branch") or "")
                        if not dependency_task_id or not dependency_branch:
                            continue
                        try:
                            dependency_workspace = backend.load_task_workspace(
                                root=paths.root,
                                run_dir=paths.run_dir,
                                task_id=dependency_task_id,
                                branch=dependency_branch,
                                base_branch=selected_base,
                            )
                        except GitOpsError as e:
                            deferred_dependency_error = (
                                "deferred expected-red regression dependency workspace "
                                f"could not be loaded: {e}"
                            )
                            _mark_status(paths, plan, dependency_task_id, "failed")
                            execution_skipped[dependency_task_id] = deferred_dependency_error
                            _trace(
                                "worktree.error",
                                deferred_dependency_error,
                                task_id=dependency_task_id,
                            )
                            continue
                        deferred_dependency_items.append(
                            ReadyBatchItem(
                                task=dependency_task,
                                prepared_workspace=dependency_workspace,
                                changed_files=tuple(
                                    _load_worker_changed_files(paths, dependency_task_id)
                                ),
                                report_path_raw=os.fspath(
                                    paths.execution_reports_dir / f"{dependency_task_id}.md"
                                ),
                            )
                        )
                    if deferred_dependency_items:
                        dependency_ids = ", ".join(
                            item.task_id for item in deferred_dependency_items
                        )
                        batch_ids = ", ".join(item.task_id for item in ready_items)
                        _trace(
                            "merge.lifecycle",
                            (
                                "Including deferred expected-red regression dependency "
                                f"{dependency_ids} with ready implementation batch {batch_ids}."
                            ),
                            verbosity="full",
                        )
                        ready_items = [*deferred_dependency_items, *ready_items]
                    if deferred_dependency_error is not None:
                        batch_task_attempt_resolutions = (
                            _reject_ready_batch_items(
                                paths=paths,
                                plan=plan,
                                backend=backend,
                                keep_worktrees=keep_worktrees,
                                ready_items=ready_items,
                                reason=deferred_dependency_error,
                                merge_outcomes=merge_outcomes,
                                execution_skipped=execution_skipped,
                                cleanup_nonmerged_workspace=_cleanup_nonmerged_workspace,
                            )
                            or batch_task_attempt_resolutions
                        )
                        if batch_task_attempt_resolutions:
                            rebuild_knowledge_index(paths)
                        continue
                    integration_gate_index += 1
                    batch_task_ids = [item.task_id for item in ready_items]
                    batch_integration_result, batch_rejection_reason = (
                        _verify_ready_batch_candidate(
                            paths=paths,
                            plan=plan,
                            cfg=cfg,
                            workspace_scan=workspace_scan,
                            backend=backend,
                            batch_index=integration_gate_index,
                            base_branch=selected_base,
                            integration_mode=effective_integration_mode,
                            allow_transient_dependent_integration_warning=(
                                effective_replanning_mode == "off"
                            ),
                            integration_verify_cmd=integration_verify_cmd,
                            verify_cmd=verify_cmd,
                            keep_worktrees=keep_worktrees,
                            ready_items=ready_items,
                            trace=lambda phase, message: _trace(phase, message),
                            task_trace=lambda task_id, message: _trace(
                                "integration.error", message, task_id=task_id
                            ),
                            integration_runner=integration_runner,
                        )
                    )
                    if batch_integration_result is not None:
                        integration_results.append(batch_integration_result)
                    if (
                        batch_integration_result is not None
                        and not batch_integration_result.passed
                        and batch_rejection_reason is None
                    ):
                        tolerated_integration_failures.append(batch_integration_result)
                        record_integration_failure_knowledge(
                            paths=paths,
                            result=batch_integration_result,
                        )
                    if batch_rejection_reason is not None:
                        if batch_integration_result is not None:
                            record_integration_failure_knowledge(
                                paths=paths,
                                result=batch_integration_result,
                            )
                        batch_task_attempt_resolutions = (
                            _reject_ready_batch_items(
                                paths=paths,
                                plan=plan,
                                backend=backend,
                                keep_worktrees=keep_worktrees,
                                ready_items=ready_items,
                                reason=batch_rejection_reason,
                                merge_outcomes=merge_outcomes,
                                execution_skipped=execution_skipped,
                                cleanup_nonmerged_workspace=_cleanup_nonmerged_workspace,
                            )
                            or batch_task_attempt_resolutions
                        )
                        if batch_promoted_knowledge or batch_task_attempt_resolutions:
                            rebuild_knowledge_index(paths)
                        if batch_integration_result is not None:
                            batch_replan_attempt = _maybe_run_replanning(
                                batch_index=integration_gate_index,
                                merged_task_ids=batch_task_ids,
                                integration_result=batch_integration_result,
                                force_suggest=effective_integration_mode == "strict",
                            )
                            if batch_replan_attempt is not None:
                                schedule_recompute_requested = (
                                    batch_replan_attempt.applied
                                    and batch_replan_attempt.plan_changed
                                )
                                _record_replanning_attempt(
                                    batch_replan_attempt,
                                    schedule_recomputed=schedule_recompute_requested,
                                )
                        if effective_integration_mode == "strict":
                            integration_blocked = True
                            blocked_label = (
                                batch_integration_result.batch_label
                                if batch_integration_result is not None
                                else f"batch_{integration_gate_index:03d}"
                            )
                            reason = (
                                f"blocked by strict integration gate {blocked_label}: "
                                f"{batch_rejection_reason}"
                            )
                            blocked_task_ids = _mark_remaining_tasks_blocked_by_integration(
                                paths=paths,
                                plan=plan,
                                reason=reason,
                                retry_failed=retry_failed,
                                retry_changes_requested=retry_changes_requested,
                                parallel=parallel,
                                max_attempts=effective_max_attempts,
                                only_ids=only_ids,
                            )
                            for task_id in blocked_task_ids:
                                execution_skipped[task_id] = reason
                                _trace("integration.error", reason, task_id=task_id)
                            break
                    else:
                        (
                            batch_merged_task_ids,
                            batch_remote_task_ids,
                            batch_merged_paths,
                            merged_batch_promoted_knowledge,
                            merged_batch_task_attempt_resolutions,
                        ) = _merge_ready_batch_items_into_base(
                            paths=paths,
                            plan=plan,
                            cfg=cfg,
                            api_key_override=api_key_override,
                            backend=backend,
                            base_branch=selected_base,
                            keep_worktrees=keep_worktrees,
                            verify_mode=verify_mode,
                            verify_commands=_get_verify_commands(),
                            verify_command_selection=(
                                _get_verify_command_selection() if verify_mode != "off" else None
                            ),
                            ready_items=ready_items,
                            merge_outcomes=merge_outcomes,
                            trace=_trace,
                            cleanup_nonmerged_workspace=_cleanup_nonmerged_workspace,
                        )
                        batch_promoted_knowledge = (
                            batch_promoted_knowledge or merged_batch_promoted_knowledge
                        )
                        batch_task_attempt_resolutions = (
                            batch_task_attempt_resolutions or merged_batch_task_attempt_resolutions
                        )
                        final_gate_task_ids.extend(batch_merged_task_ids)
                        final_gate_paths.extend(batch_merged_paths)
                        if remote_settings.enabled and batch_remote_task_ids:
                            _trace(
                                "remote.lifecycle",
                                f"Pushing updated base branch {selected_base}.",
                                verbosity="full",
                            )
                            pushed_base, base_output = push_base(
                                paths.root,
                                remote=remote_settings.remote_name,
                                base_branch=selected_base,
                            )
                            for task_id in batch_remote_task_ids:
                                record_path = paths.execution_dir / "remote" / f"{task_id}.json"
                                if not record_path.exists():
                                    continue
                                payload = json.loads(record_path.read_text(encoding="utf-8"))
                                payload["pushed_base"] = pushed_base
                                payload["base_push_output"] = truncate_output(base_output)
                                if not pushed_base:
                                    errors = payload.setdefault("errors", [])
                                    if isinstance(errors, list):
                                        errors.append(
                                            "remote base push failed: "
                                            f"{base_output or 'unknown error'}"
                                        )
                                        _trace(
                                            "remote.error",
                                            f"Base push failed: {base_output or 'unknown error'}",
                                            task_id=task_id,
                                        )
                                write_remote_record(
                                    execution_dir=paths.execution_dir,
                                    task_id=task_id,
                                    record=payload,
                                )
                                task_result = next(
                                    (r for r in ready_for_merge_results if r.task_id == task_id),
                                    None,
                                )
                                if task_result is not None:
                                    _append_remote_report_update(
                                        paths=paths,
                                        report_path_raw=task_result.report_path,
                                        record=payload,
                                    )
                        if batch_promoted_knowledge or batch_task_attempt_resolutions:
                            rebuild_knowledge_index(paths)
                        if (
                            batch_integration_result is not None
                            and batch_integration_result.passed
                            and len(batch_merged_task_ids) == len(batch_task_ids)
                        ):
                            record_integration_resolution_knowledge(
                                paths=paths,
                                result=batch_integration_result,
                            )
                            batch_replan_attempt = _maybe_run_replanning(
                                batch_index=integration_gate_index,
                                merged_task_ids=batch_task_ids,
                                integration_result=batch_integration_result,
                                force_suggest=False,
                            )
                            if batch_replan_attempt is not None:
                                schedule_recompute_requested = (
                                    batch_replan_attempt.applied
                                    and batch_replan_attempt.plan_changed
                                )
                                _record_replanning_attempt(
                                    batch_replan_attempt,
                                    schedule_recomputed=schedule_recompute_requested,
                                )
                                if schedule_recompute_requested:
                                    _trace(
                                        "scheduler.lifecycle",
                                        (
                                            "Recomputing remaining schedule after applied "
                                            "replanning changed the canonical plan "
                                            f"({batch_replan_attempt.replan_label})."
                                        ),
                                    )
                                    break

                if integration_blocked:
                    break
                if schedule_recompute_requested:
                    break
            if integration_blocked:
                break
            if schedule_recompute_requested:
                continue

        if (
            tolerated_integration_failures
            and not integration_blocked
            and not _cancellation_requested()
            and merge_strategy == "auto"
            and effective_integration_mode != "off"
            and final_gate_task_ids
        ):
            integration_gate_index += 1
            final_task_ids = list(dict.fromkeys(final_gate_task_ids))
            final_paths = list(dict.fromkeys(final_gate_paths))
            final_integration_result = _run_batch_integration_gate(
                paths=paths,
                cfg=cfg,
                workspace_scan=workspace_scan,
                batch_index=integration_gate_index,
                integration_mode=effective_integration_mode,
                integration_verify_cmd=integration_verify_cmd,
                verify_cmd=verify_cmd,
                merged_task_ids=final_task_ids,
                merged_paths=final_paths,
                root=paths.root,
                phase="final_repo",
                trace=lambda phase, message: _trace(phase, message),
                task_trace=lambda task_id, message: _trace(
                    "integration.error",
                    message,
                    task_id=task_id,
                ),
                integration_runner=integration_runner,
            )
            if final_integration_result is not None:
                integration_results.append(final_integration_result)
                if final_integration_result.passed:
                    record_integration_resolution_knowledge(
                        paths=paths,
                        result=final_integration_result,
                    )
                else:
                    record_integration_failure_knowledge(
                        paths=paths,
                        result=final_integration_result,
                    )

        run_outcome = _compute_swarm_run_outcome(
            plan=plan,
            integration_results=integration_results,
            worker_verification_warnings=bool(worker_verification_warnings),
            review_blocked=review_blocked,
            remote_blocked_any=remote_blocked_any,
            integration_blocked=integration_blocked,
            only=only,
            max_tasks=max_tasks,
            dry_run=False,
            interrupted=_cancellation_requested() or bool(interrupted_tasks),
            interrupted_task_ids=tuple(sorted(interrupted_tasks)),
            review_merge_strategy=merge_strategy == "review",
        )
        _write_swarm_summary_json(
            paths=paths,
            outcome=run_outcome,
            integration_results=integration_results,
        )
        summary_path = _write_swarm_summary(
            paths=paths,
            backend_name=backend.name,
            base_branch=selected_base,
            executed=executed,
            merge_outcomes=merge_outcomes,
            integration_results=integration_results,
            worker_verification_warnings=worker_verification_warnings,
            replanning_results=replanning_results,
            skipped=_compose_summary_skipped(
                scheduler_skipped=scheduler_skipped,
                execution_skipped=execution_skipped,
                observed_task_ids=observed_task_ids,
            ),
            recovered=recovered_tasks,
            startup_warnings=startup_warnings,
            dry_run=False,
            schedule_preview=executed_batch_history,
            workspace_summary_lines=workspace_summary_lines,
            binding_summary_lines=binding_summary_lines,
            run_outcome=run_outcome,
        )
        exit_code = run_outcome.exit_code

        _trace(
            "swarm.lifecycle",
            (
                f"Swarm completed with exit code {exit_code}. Status: "
                f"{run_outcome.status}. Summary: {summary_path}."
            ),
        )
        return exit_code
    except Exception as e:
        _trace("swarm.error", f"Swarm aborted: {e}")
        raise
    finally:
        try:
            sink.close()
        finally:
            if owns_run_mutation_guard:
                run_mutation_guard.release()
