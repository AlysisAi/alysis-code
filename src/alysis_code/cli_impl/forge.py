# ruff: noqa: F821
# Dependencies are injected at runtime from alysis_code.cli to preserve monkeypatch surfaces.
from __future__ import annotations

import inspect
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from click import get_current_context
from click.core import ParameterSource

from ..assets import AssetError
from ..assets.budget_allocator import (
    TaskAssetAllocation,
    allocate_task_assets,
    write_task_asset_allocation,
)
from ..assets.surface import build_asset_surface
from ..assets.usage_logger import AssetUsageLogger
from ..assets.worker_mirror import TaskAssetMirror, mirror_task_assets
from ..assets.worker_section import render_relevant_assets_section
from ..assets.worker_tools import build_worker_asset_mcp_manager, compose_worker_asset_mcp_manager
from ..branding import env_get
from ..error_text import sanitize_error_summary, sanitize_optional_error_summary
from ..failure_category import FailureCategory
from ..forge_events import (
    EVENT_PLAN_INVALID,
    EVENT_PLAN_SAVED,
    EVENT_REVIEW_RESULT,
    EVENT_SCOPE_AMENDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_STARTED,
    EVENT_VERIFICATION_RESULT,
    EVENT_VERIFICATION_UNAVAILABLE,
    EXIT_ERROR,
    EXIT_NOT_ACCEPTED,
    EXIT_OK,
    ForgeEventEmitter,
)
from ..model_registry import ModelRegistry
from ..plan_repair import (
    PLAN_STATUS_DRAFT,
    apply_plan_status,
    plan_repair_event_payload,
)
from ..plan_validation import PlannerFailedError, raise_for_execution_ready_plan
from ..replanning import resolve_replanning_mode
from ..run_state import RUN_STATUS_FAILED, RUN_STATUS_RUNNING
from ..runtime_kind import RuntimeKind
from ..swarm_orchestrator import acquire_swarm_mutation_guard
from ..swarm_scheduler import (
    SUCCESSFUL_TERMINAL_STATUSES,
    TaskCandidate,
    canonical_task_status,
    select_task_candidates,
)
from ..task_readiness import is_clearly_non_mutating_task
from ..task_scope import (
    apply_scope_amendments,
    assess_scope_changes,
    describe_scope_violations,
    is_non_material_untracked_path,
    normalize_scope_patterns,
    relocate_known_scratch_artifacts,
)
from ..verification_repair import (
    TASK_STATUS_COMPLETED_UNVERIFIED,
    RepairAttemptExecution,
    build_repair_instruction,
    resolve_repair_attempt_budget,
    run_verification_repair_loop,
    verification_failure_excerpts,
)

_PROTECTED_GLOBAL_NAMES: set[str] = set()


def _events_or_null(events: Any) -> ForgeEventEmitter:
    """Return the caller's emitter, or an inert one so call sites stay unconditional."""
    if isinstance(events, ForgeEventEmitter):
        return events
    return ForgeEventEmitter(command="forge", enabled=False)


@dataclass
class TaskExecutionOutcome:
    """What one run of :func:`execute_forge_task` produced.

    ``forge exec`` turns a single outcome into its exit code and terminal event;
    ``forge run`` collects one per task and decides whether the run continues.
    """

    task_id: str
    title: str
    status: str
    success: bool
    exit_code: int
    summary: str
    report_path: Path
    patch_path: Path
    merge_conflict: bool
    conflict_review_path: Path | None
    payload: dict[str, Any] = field(default_factory=dict)


class ForgeTaskExecutionError(Exception):
    """The task could not be executed at all -- a command error, not a failed task.

    A task that ran and was rejected comes back as an outcome with
    ``success=False`` (exit 1). This is the other case: bad git state, missing
    branch context, nothing to execute against. Callers report it as exit 2, and
    a sequential run stops rather than moving to the next task.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "forge_error",
        task_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = str(kind)
        self.task_id = task_id


def _sequential_conflict_report_lines(
    *,
    root: Path,
    task_id: str,
    base_branch: str | None,
    task_branch: str | None,
    review_path: Path | None,
) -> list[str]:
    """Describe an unresolved merge conflict and how to finish it.

    The sequential path stops at a conflict on purpose instead of starting the
    worktree-based resolver agent, so this report has to carry everything the
    next step needs: where the conflict is, how to land it by hand, and how to
    resume the run.
    """
    base = base_branch or "the base branch"
    branch = task_branch or "the task branch"
    workspace = os.fspath(root)
    lines = [
        f"Merge conflict: {branch} did not merge into {base}. The sequential run "
        "stopped here and started no resolver agent.",
    ]
    if review_path is not None:
        lines.append(f"Conflict review: {os.fspath(review_path)}")
    lines.append(
        f"Finish it by hand: `git -C {workspace} checkout {base}` then "
        f"`git -C {workspace} merge --no-ff {branch}`, resolve the conflicted "
        "files, and commit."
    )
    lines.append(f"Then continue the remaining tasks with `alysis forge run --path {workspace}`.")
    lines.append(
        "To let an agent attempt the resolution instead, re-run this task with "
        f"`alysis forge exec {task_id} --pr --auto-resolve-conflicts "
        f"--path {workspace}`."
    )
    return lines


# --- sequential run scheduling -------------------------------------------------
#
# `forge run` reuses the swarm scheduler's readiness rules (dependencies, retry
# eligibility, attempt limits) but never its batching: the next task is simply the
# first ready one, recomputed after each task so a task that just finished unblocks
# its dependents.

_NON_EXECUTABLE_TASK_STATUSES = frozenset({"superseded", "invalidated"})


def _parse_only_task_ids(only: str | None) -> set[str] | None:
    if not isinstance(only, str):
        return None
    ids = {part.strip() for part in only.split(",") if part.strip()}
    return ids or None


def _plan_task_dicts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def _sequential_candidates(
    plan: dict[str, Any],
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
    max_attempts: int | None,
    only_ids: set[str] | None,
    exclude_ids: set[str] | None = None,
) -> list[TaskCandidate]:
    runnable, _ready_for_merge, _skipped = select_task_candidates(
        tasks=_plan_task_dicts(plan),
        retry_failed=retry_failed,
        retry_changes_requested=retry_changes_requested,
        max_attempts=max_attempts,
        only_ids=only_ids,
    )
    excluded = exclude_ids or set()
    return [candidate for candidate in runnable if candidate.task_id not in excluded]


def _next_sequential_task(
    plan: dict[str, Any],
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
    max_attempts: int | None,
    only_ids: set[str] | None,
    exclude_ids: set[str] | None = None,
) -> TaskCandidate | None:
    """First task whose dependencies are satisfied, or None when none are ready.

    ``exclude_ids`` holds what this run already attempted. Without it a retried
    task that fails again would be re-selected forever, because failing is exactly
    the status ``--retry-failed`` asks the scheduler to accept.
    """
    candidates = _sequential_candidates(
        plan,
        retry_failed=retry_failed,
        retry_changes_requested=retry_changes_requested,
        max_attempts=max_attempts,
        only_ids=only_ids,
        exclude_ids=exclude_ids,
    )
    return candidates[0] if candidates else None


def _projected_sequential_order(
    plan: dict[str, Any],
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
    max_attempts: int | None,
    only_ids: set[str] | None,
    max_tasks: int | None,
) -> list[str]:
    """The order tasks would run in, assuming each one succeeds.

    Dependencies only unblock as tasks finish, so the order has to be simulated
    rather than read off a single scheduling pass. Shallow task copies are enough:
    the simulation only rewrites ``status``.
    """
    tasks = [dict(task) for task in _plan_task_dicts(plan)]
    simulated = {"tasks": tasks}
    order: list[str] = []
    seen: set[str] = set()
    while max_tasks is None or len(order) < max_tasks:
        candidate = _next_sequential_task(
            simulated,
            retry_failed=retry_failed,
            retry_changes_requested=retry_changes_requested,
            max_attempts=max_attempts,
            only_ids=only_ids,
            exclude_ids=seen,
        )
        if candidate is None:
            break
        order.append(candidate.task_id)
        seen.add(candidate.task_id)
        for task in tasks:
            if str(task.get("id") or "").strip() == candidate.task_id:
                task["status"] = "done"
                break
    return order


def _unfinished_task_ids(plan: dict[str, Any]) -> list[str]:
    """Tasks that still represent outstanding work after the run."""
    unfinished: list[str] = []
    for task in _plan_task_dicts(plan):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        if status in SUCCESSFUL_TERMINAL_STATUSES or status in _NON_EXECUTABLE_TASK_STATUSES:
            continue
        unfinished.append(task_id)
    return unfinished


def _bump_task_attempt(paths: Any, plan: dict[str, Any], task: dict[str, Any]) -> int:
    """Record that this run is about to try the task, so --max-attempts can bite."""
    raw = task.get("attempts")
    try:
        attempts = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        attempts = 0
    attempts = max(0, attempts) + 1
    task["attempts"] = attempts
    save_plan(paths, plan)
    return attempts


class _SequentialRunLog:
    """One appended line per lifecycle step of a sequential run.

    Per-task agent logs stay where they are; this is the run-level narrative, in
    order, in one file -- the thing a swarm run cannot have because its tasks
    interleave.
    """

    def __init__(self) -> None:
        self._path: Path | None = None

    def bind(self, paths: Any) -> None:
        ensure_execution_dirs(paths)
        self._path = paths.execution_dir / "sequential_run.jsonl"

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        record: dict[str, Any] = {"ts": now_iso(), "event": str(event)}
        record.update(fields)
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
        except OSError:
            # A run log that cannot be written must not take the run down with it.
            return


def _sequential_outcome_payload(outcome: TaskExecutionOutcome) -> dict[str, Any]:
    return {
        "task_id": outcome.task_id,
        "title": outcome.title,
        "status": outcome.status,
        "success": bool(outcome.success),
        "summary": outcome.summary,
        "report": os.fspath(outcome.report_path),
        "patch": os.fspath(outcome.patch_path),
        "merge_conflict": bool(outcome.merge_conflict),
        "verify_summary": outcome.payload.get("verify_summary"),
        "branch": outcome.payload.get("branch"),
        "commit": outcome.payload.get("commit"),
        "merge_result": outcome.payload.get("merge_result"),
    }


def _write_sequential_summary(
    *,
    paths: Any,
    run_id: str,
    started_at: str,
    outcomes: list[TaskExecutionOutcome],
    stopped_reason: str,
    exit_code: int,
    pr: bool,
    verify_mode: str,
    scope_mode: str,
    remaining: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Persist the run outcome next to the swarm's, in the same execution dir."""
    executed = [_sequential_outcome_payload(item) for item in outcomes]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "command": "forge.run",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": now_iso(),
        "engine": "sequential",
        "worktrees_used": False,
        "parallel": 1,
        "git_flow": bool(pr),
        "verify": verify_mode,
        "scope": scope_mode,
        "stopped_reason": stopped_reason,
        "exit_code": int(exit_code),
        "executed": executed,
        "counts": {
            "executed": len(executed),
            "succeeded": sum(1 for item in executed if item["success"]),
            "failed": sum(1 for item in executed if not item["success"]),
        },
        "remaining": list(remaining or []),
    }
    if error:
        payload["error"] = error
    summary_path = paths.execution_dir / "sequential_summary.json"
    try:
        ensure_execution_dirs(paths)
        summary_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        payload["summary_path"] = os.fspath(summary_path)
    except OSError:
        # Reporting the run must not be able to fail the run.
        pass
    return payload


def _print_sequential_order(console: Any, plan: dict[str, Any], order: list[str]) -> None:
    table = _Table(title="Execution order")
    table.add_column("#")
    table.add_column("task")
    table.add_column("title")
    if not order:
        console.print("[yellow]No ready tasks to execute.[/yellow]")
        return
    titles = {
        str(task.get("id") or "").strip(): str(task.get("title") or "").strip()
        for task in _plan_task_dicts(plan)
    }
    for index, task_id in enumerate(order, start=1):
        table.add_row(str(index), task_id, titles.get(task_id, ""))
    console.print(table)


def _print_sequential_summary(console: Any, payload: dict[str, Any]) -> None:
    counts = payload.get("counts") or {}
    table = _Table(title="forge run")
    table.add_column("task")
    table.add_column("status")
    table.add_column("report")
    for item in payload.get("executed") or []:
        table.add_row(
            str(item.get("task_id") or ""),
            str(item.get("status") or ""),
            str(item.get("report") or ""),
        )
    console.print(table)
    console.print(
        f"Executed: {counts.get('executed', 0)} | "
        f"succeeded: {counts.get('succeeded', 0)} | "
        f"failed: {counts.get('failed', 0)} | "
        f"stopped: {payload.get('stopped_reason')}"
    )
    remaining = payload.get("remaining") or []
    if remaining:
        preview = ", ".join(str(item) for item in remaining[:10])
        if len(remaining) > 10:
            preview += ", ..."
        console.print(f"[yellow]Unfinished tasks:[/yellow] {preview}")
    # A stopped run leaves the checkout wherever the failing task left it, which is
    # the first thing you need to know before touching the repository again.
    if payload.get("stopped_reason") == "task_failed":
        failed = next(
            (item for item in reversed(payload.get("executed") or []) if not item.get("success")),
            None,
        )
        if failed is not None:
            branch = str(failed.get("branch") or "").strip()
            location = f" on branch {branch}" if branch else ""
            console.print(
                f"[yellow]Run stopped at {failed.get('task_id')}[/yellow]"
                f"{location}; nothing after it was started. "
                "Fix it and re-run `forge run` to continue, or pass --keep-going to skip "
                "past failures next time."
            )
    summary_path = payload.get("summary_path")
    if summary_path:
        console.print(f"Summary: {summary_path}")


def _exec_error_exit(
    console: Any,
    events: ForgeEventEmitter,
    message: str,
    *,
    task_id: str | None = None,
    kind: str = "forge_error",
) -> typer.Exit:
    """Report a `forge exec` command error on both surfaces and build the Exit."""
    console.print(f"[red]Forge error:[/red] {message}")
    events.error(
        message=str(message),
        kind=kind,
        exit_code=EXIT_ERROR,
        data={"task_id": task_id} if task_id else None,
    )
    return typer.Exit(code=EXIT_ERROR)


def _emit_verification_unavailable(
    events: ForgeEventEmitter,
    *,
    task_id: str,
    policy: str,
    reason: str,
    blocking: bool,
    outcome: str | None = None,
) -> None:
    """Report that verification did not run, and whether that blocked the task.

    ``outcome`` names the task status this produced, so a machine consumer can tell
    "kept, but nothing checked it" (``completed_unverified``) apart from the merely
    informational unavailable notices that leave the outcome untouched.
    """
    payload: dict[str, Any] = {
        "scope": "task",
        "task_id": task_id,
        "policy": policy,
        "reason": reason,
        "blocking": bool(blocking),
    }
    if outcome:
        payload["outcome"] = str(outcome)
    events.emit(EVENT_VERIFICATION_UNAVAILABLE, payload)


def _attempt_verification_repair(
    *,
    attempt: int,
    failing_result: Any,
    max_attempts: int,
    task_id: str,
    base_instruction: str,
    verify_artifact_path: Path,
    run_repair_agent: Any,
    run_verification: Any,
    root: Path,
    commit_message: str,
) -> RepairAttemptExecution:
    """Run one repair attempt: prompt with the failure, fix, commit, re-verify.

    Nothing here decides the task's outcome -- it reports what happened and lets
    :func:`run_verification_repair_loop` decide whether to keep going.
    """
    instruction = build_repair_instruction(
        base_instruction=base_instruction,
        task_id=task_id,
        attempt=attempt,
        max_attempts=max_attempts,
        verify_summary=str(getattr(failing_result, "summary", "")),
        excerpts=verification_failure_excerpts(failing_result),
        artifact_path=os.fspath(verify_artifact_path),
    )
    try:
        exit_code = int(run_repair_agent(instruction, attempt))
    except Exception as e:  # noqa: BLE001
        return RepairAttemptExecution(
            agent_exit_code=1,
            verify_result=None,
            error=f"repair agent raised: {sanitize_error_summary(str(e))}",
        )
    if exit_code != 0:
        # A crashed repair agent leaves the tree in an unknown state; re-verifying it
        # would attribute its mess to the task's own work.
        return RepairAttemptExecution(
            agent_exit_code=exit_code,
            verify_result=None,
            error=f"repair agent exited non-zero ({exit_code})",
        )

    committed = False
    try:
        commit_hash = _stage_and_commit_task_changes(root, message=commit_message)
        committed = commit_hash is not None
    except GitOpsError as e:
        return RepairAttemptExecution(
            agent_exit_code=exit_code,
            verify_result=None,
            error=f"could not commit the repair attempt: {e}",
        )
    if not committed:
        # The agent changed nothing, so re-running the same commands would produce
        # the same failure. Stop rather than burn the rest of the budget.
        return RepairAttemptExecution(
            agent_exit_code=exit_code,
            verify_result=None,
            error="the repair attempt made no repository changes",
        )

    changed_files: tuple[str, ...] = ()
    try:
        changed_files = tuple(changed_files_between(root, revspec="HEAD~1..HEAD"))
    except GitOpsError:
        changed_files = ()

    return RepairAttemptExecution(
        agent_exit_code=exit_code,
        verify_result=run_verification(),
        committed=True,
        changed_files=changed_files,
    )


def _stage_and_commit_task_changes(root: Path, *, message: str) -> str | None:
    """Stage the workspace and commit it, skipping protected and non-material paths.

    Returns the new commit hash, or ``None`` when there was nothing to commit. Used
    by verification repair attempts, which run after the task's own commit and must
    land their fix on the same branch before verification is re-run.
    """
    non_material_untracked_paths = list_untracked_packaging_metadata_paths(root)
    stage_all(root)
    unstage_staged_prefixes(root, [".alysis", ".alysis_images", "alysis-feedback"])
    ensure_not_staged_prefixes(root, [".alysis", ".alysis_images", "alysis-feedback"])
    if non_material_untracked_paths:
        unstage_staged_paths(root, non_material_untracked_paths)
        ensure_not_staged_paths(root, non_material_untracked_paths)
    staged_now = staged_files(root)
    if staged_now and has_grounded_rust_target_runtime_artifacts(root):
        unstage_staged_runtime_artifacts(root, current_paths=staged_now)
        staged_now = staged_files(root)
        ensure_not_staged_runtime_artifacts(root, current_paths=staged_now)
    if not staged_now:
        return None
    return commit_all(root, message=message)


def _plan_execution_readiness(plan: dict[str, Any]) -> str | None:
    """Return why the plan cannot be executed yet, or None when it is ready."""
    try:
        return _forge_no_execution_ready_tasks_message(plan)
    except Exception:  # noqa: BLE001
        # Readiness is reporting metadata; never let it break the command it describes.
        return None


def _sync_cli_globals(cli_mod: Any) -> None:
    module_globals = globals()
    if not _PROTECTED_GLOBAL_NAMES:
        for local_name, local_value in module_globals.items():
            if callable(local_value):
                _PROTECTED_GLOBAL_NAMES.add(local_name)
    for name, value in cli_mod.__dict__.items():
        if name.startswith("__") or name in _PROTECTED_GLOBAL_NAMES:
            continue
        module_globals[name] = value


def _path_binding_source(ctx: Any = None, path: Path | None = None) -> str:
    current_ctx = ctx if ctx is not None else get_current_context(silent=True)
    path_source = current_ctx.get_parameter_source("path") if current_ctx is not None else None
    if path_source is not None and path_source is not ParameterSource.DEFAULT:
        return "explicit_path"
    if path is None and current_ctx is not None:
        raw_path = current_ctx.params.get("path")
        path = Path(raw_path) if raw_path is not None else None
    if path is None:
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        raw_path = caller.f_locals.get("path") if caller is not None else None
        path = Path(raw_path) if raw_path is not None else None
    if path is not None and Path(path) != Path("."):
        return "explicit_path"

    if path_source is None or path_source is ParameterSource.DEFAULT:
        return "cwd"
    return "explicit_path"


def _mark_run_status(
    paths: Any,
    status: str,
    *,
    reason: str = "",
    plan: dict[str, Any] | None = None,
    mode: str | None = None,
) -> None:
    """Record a run lifecycle transition on the current-run pointer.

    Best-effort by design: a pointer that cannot be written is a stale status line
    in a UI, never a reason to fail the execution it describes.
    """
    from ..forge import set_current_run_status
    from ..run_state import RUN_STATUS_RUNNING, build_run_owner

    owner = None
    if status == RUN_STATUS_RUNNING:
        owner = build_run_owner(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            mode=mode or "forge",
        )
    try:
        set_current_run_status(paths, status, reason=reason, owner=owner, plan=plan)
    except Exception:  # noqa: BLE001 - see docstring
        return


def _mark_run_interrupted_if_still_running(paths: Any, *, reason: str) -> None:
    """Close out a run whose process is leaving without having recorded an outcome.

    Every normal exit path stamps a terminal status first, so reaching here means
    an exception, a ``KeyboardInterrupt``, or a code path that forgot -- all of
    which leave resumable work behind, which is what ``interrupted`` means.
    """
    from ..forge import current_run_status
    from ..run_state import RUN_STATUS_INTERRUPTED, RUN_STATUS_RUNNING

    try:
        if current_run_status(paths.root) != RUN_STATUS_RUNNING:
            return
    except Exception:  # noqa: BLE001
        return
    _mark_run_status(paths, RUN_STATUS_INTERRUPTED, reason=reason)


def _terminal_run_status_for(*, plan: dict[str, Any], any_failure: bool) -> tuple[str, str]:
    """Map an execution's result onto the run lifecycle enum.

    Unfinished tasks left by ``--only`` or ``--max-tasks`` are a scope the caller
    chose, not lost work, so they land back on ``approved`` (executable, idle)
    rather than ``interrupted`` (something went wrong).
    """
    from ..run_state import RUN_STATUS_APPROVED, RUN_STATUS_COMPLETED

    if any_failure:
        return RUN_STATUS_FAILED, "execution finished with at least one task not accepted"
    remaining = _unfinished_task_ids(plan)
    if remaining:
        return (
            RUN_STATUS_APPROVED,
            f"execution finished; {len(remaining)} task(s) still unexecuted",
        )
    return RUN_STATUS_COMPLETED, "every plan task reached a successful terminal status"


def _missing_swarm_run_error(*, binding: WorkspaceBinding) -> ForgeError:
    requested = os.fspath(binding.requested_path)
    return ForgeError(
        "No current forge run was found for this workspace. "
        f"Start with `alysis forge plan --path {requested}` or enter Forge "
        "from chat after starting alysis inside a project folder."
    )


def _print_forge_lock_wait_notice(console: Any, info: dict[str, Any]) -> None:
    diagnostic = str(info.get("diagnostic") or "").strip()
    console.print(
        "[yellow]Forge execution queued:[/yellow] another execution is mutating this workspace; waiting for it to finish."
    )
    if diagnostic:
        console.print(f"[dim]{diagnostic}[/dim]")


def _render_planner_reply(
    *, console: Any, message: str, questions: list[str] | None = None
) -> None:
    console.print("[bold]Planner:[/bold]")
    console.print(message)
    if questions:
        console.print("[dim]Planner questions[/dim]")
        for question in questions:
            console.print(f"- {question}")


def _merge_changed_files(*path_groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in path_groups:
        for raw in group:
            value = str(raw).strip().replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


def _runtime_snapshot_changed_files(
    before_snapshot: dict[str, str],
    after_snapshot: dict[str, str],
) -> list[str]:
    changed: list[str] = []
    for path in sorted(set(before_snapshot) | set(after_snapshot)):
        if before_snapshot.get(path) != after_snapshot.get(path):
            changed.append(_normalize_changed_file_path(path))
    return [path for path in changed if path]


def _normalize_changed_file_path(path: str) -> str:
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _authorized_custom_tool_runtime_side_effects(
    *,
    sessions_dir: Path,
    session_id: str,
) -> set[str]:
    session_log = sessions_dir / f"{session_id}.jsonl"
    if not session_log.exists():
        return set()
    authorized: set[str] = set()
    try:
        lines = session_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "tool_result":
            continue
        payload = event.get("payload")
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or result.get("success") is not True:
            continue
        side_effects = result.get("side_effects")
        if not isinstance(side_effects, dict):
            continue
        writes = side_effects.get("workspace_writes")
        if not isinstance(writes, list):
            continue
        for item in writes:
            if not isinstance(item, dict):
                continue
            if str(item.get("scope") or "") != "tool_dir":
                continue
            rel_path = _normalize_changed_file_path(str(item.get("path") or ""))
            if rel_path == ".alysis/tools" or rel_path.startswith(".alysis/tools/"):
                authorized.add(rel_path)
    return authorized


def _path_matches_authorized_runtime_prefix(path: str, prefixes: set[str]) -> bool:
    normalized = _normalize_changed_file_path(path)
    return any(
        normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    )


def _drop_parent_directory_placeholders(paths: list[str]) -> list[str]:
    concrete_paths = [path for path in paths if path and not path.endswith("/")]
    filtered: list[str] = []
    for path in paths:
        if path.endswith("/") and any(other.startswith(path) for other in concrete_paths):
            continue
        filtered.append(path)
    return filtered


def _task_declares_explicit_write_scope(task: dict[str, Any]) -> bool:
    raw = task.get("write_scope")
    if isinstance(raw, list):
        return any(str(item or "").strip() for item in raw)
    if isinstance(raw, str):
        return bool(raw.strip())
    return False


def _task_is_analysis_only(task: dict[str, Any]) -> bool:
    raw_flag = task.get("analysis_only")
    if isinstance(raw_flag, bool):
        return raw_flag
    return is_clearly_non_mutating_task(
        title=str(task.get("title") or "").strip(),
        description=str(task.get("description") or "").strip(),
        acceptance_criteria=[
            str(item or "")
            for item in (task.get("acceptance_criteria") or [])
            if str(item or "").strip()
        ],
    )


def _task_string_list(task: dict[str, Any], key: str) -> list[str]:
    raw = task.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if not isinstance(raw, (list, tuple)):
        text = str(raw).strip()
        return [text] if text else []
    return [str(item).strip() for item in raw if str(item).strip()]


def _empty_forge_exec_task_asset_mirror(
    workspace_path: Path,
    *,
    task_id: str = "",
) -> TaskAssetMirror:
    workspace = workspace_path.resolve()
    return TaskAssetMirror(
        workspace_path=workspace,
        manifest_path=workspace / ".alysis" / "task_assets" / "manifest.json",
        primary=[],
        may_need=[],
        pinned=[],
        task_id=task_id,
    )


def _combined_forge_exec_image_paths(
    *,
    legacy_paths: list[str],
    mirror: TaskAssetMirror,
    allocation: TaskAssetAllocation | None,
    cfg: Any,
    role_model: str,
    model_registry: ModelRegistry,
    usage_logger: AssetUsageLogger,
) -> list[str] | None:
    combined: list[str] = []
    seen: set[str] = set()

    def _append(path: str) -> None:
        try:
            normalized = os.fspath(Path(path).resolve())
        except OSError:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        combined.append(normalized)

    for path in legacy_paths:
        _append(path)
    if cfg.assets.worker.inline_images and model_registry.get(role_model).supports_vision:
        decision_by_id = {
            decision.asset_id: decision.mode
            for decision in (allocation.decisions if allocation else [])
        }
        max_new = max(0, int(cfg.assets.worker.max_inline_images))
        added_new = 0
        for entry in mirror.primary:
            if entry.kind != "image" or entry.status != "mirrored":
                continue
            if decision_by_id.get(entry.asset_id) not in {"full_inline", "focused_extract"}:
                continue
            if entry.raw_workspace_path is None or not entry.raw_workspace_path.exists():
                continue
            before = len(combined)
            _append(os.fspath(entry.raw_workspace_path))
            if len(combined) > before:
                usage_logger.inline_injection(asset_id=entry.asset_id, kind=entry.kind)
                added_new += 1
                if added_new >= max_new:
                    break
    max_total = max(0, int(cfg.assets.worker.max_inline_images))
    if max_total > 0:
        combined = combined[:max_total]
    return combined or None


def _append_patch_debug_section(patch_path: Path, *, title: str, patch_text: str) -> None:
    existing = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
    parts: list[str] = []
    if existing:
        parts.append(existing if existing.endswith("\n") else existing + "\n")
    parts.append(f"# {title}\n")
    if patch_text:
        parts.append(patch_text if patch_text.endswith("\n") else patch_text + "\n")
    patch_path.write_text("\n".join(part.rstrip("\n") for part in parts if part), encoding="utf-8")


def forge_plan(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    create_path: bool = typer.Option(
        False,
        "--create-path",
        help="Create --path if it does not exist before binding the workspace.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of choosing a narrower project folder.",
    ),
    cli_ctx: Any = None,
    events: Any = None,
) -> None:
    events = _events_or_null(events)
    console = _console()
    try:
        binding = _resolve_startup_workspace_binding(
            requested_path=path,
            console=console,
            interactive=not _is_non_interactive_terminal(),
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            source=_path_binding_source(cli_ctx),
            action=WorkspaceAction.FORGE_PLAN,
        )
        paths = create_plan_run(
            path,
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            workspace_binding=binding,
        )
        events.set_run_id(paths.run_id)
        plan = load_plan(paths)
        workspace_scan = ensure_workspace_context_artifacts(paths)
    except (ForgeError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(message=str(e), kind="forge_error", exit_code=EXIT_ERROR)
        raise typer.Exit(code=EXIT_ERROR) from e

    console.rule("[bold cyan]forge plan[/bold cyan]")
    console.print(f"Run ID: {paths.run_id}")
    console.print(f"Plan directory: {paths.plan_dir}")
    for line in format_workspace_context_summary_lines(workspace_scan):
        console.print(line)
    console.print("Planning loop started. Type /help for commands. Type /done to finish.")
    assistant_enabled = False
    planning_suggested: set[str] = set()
    planner_state = _ForgePlannerSessionState(
        workspace_context=(
            _workspace_context_payload_for_paths(paths=paths)
            or {
                **workspace_scan.to_dict(),
                "greenfield": bool(getattr(paths, "greenfield", False)),
            }
        )
    )

    def _emit_planner_meta(message: str) -> None:
        console.print(message)

    def _emit_planner_warning_group(label: str, warnings: list[str]) -> None:
        for warning in warnings:
            console.print(f"[yellow]{label}:[/yellow] {warning}")

    while True:
        try:
            # In machine mode the prompt goes to stderr so stdout stays pure NDJSON;
            # planning input is still read from stdin exactly as before.
            line = typer.prompt("plan", err=events.enabled)
        except (EOFError, KeyboardInterrupt):
            console.print("")
            break
        text = line.strip()
        if not text:
            continue

        append_transcript_note(paths, role="user", message=text)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"/done", "done", "/exit", "exit", "/quit", "quit"}:
            append_transcript_note(paths, role="system", message="Planning loop finished.")
            break
        if cmd in {"/help", "help"}:
            console.print(_planning_help_panel())
            append_transcript_note(paths, role="system", message="Displayed planning help.")
            continue
        if cmd == "/assistant":
            assistant_cmd = arg.lower()
            if not assistant_cmd:
                assistant_cmd, picker_available = _select_forge_assistant_interactive(
                    enabled=assistant_enabled,
                    console=console,
                )
                if not picker_available:
                    console.print("[yellow]Usage:[/yellow] /assistant on|off|status")
                    append_transcript_note(
                        paths,
                        role="system",
                        message="Rejected invalid /assistant usage.",
                    )
                    continue
                if assistant_cmd is None:
                    continue
            if assistant_cmd == "on":
                assistant_enabled = True
                console.print("Planner assistant: ON")
                append_transcript_note(paths, role="system", message="Planner assistant enabled.")
                continue
            if assistant_cmd == "off":
                assistant_enabled = False
                _set_forge_planner_follow_up_state(
                    planner_state=planner_state,
                    questions=[],
                    awaiting_clarification=False,
                )
                console.print("Planner assistant: OFF")
                append_transcript_note(paths, role="system", message="Planner assistant disabled.")
                continue
            if assistant_cmd == "status":
                state = "ON" if assistant_enabled else "OFF"
                console.print(f"Planner assistant: {state}")
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Planner assistant status requested ({state}).",
                )
                continue

            console.print("[yellow]Usage:[/yellow] /assistant on|off|status")
            append_transcript_note(
                paths,
                role="system",
                message="Rejected invalid /assistant usage.",
            )
            continue

        if assistant_enabled:
            _run_forge_planner_turn_controller(
                console=console,
                paths=paths,
                plan=plan,
                planner_state=planner_state,
                user_text=text,
                cfg_loader=load_config,
                unavailable_message_builder=(
                    lambda error: (
                        "Planner assistant is unavailable because config could not be loaded: "
                        f"{error}"
                    )
                ),
                emit_meta=_emit_planner_meta,
                emit_warning_group=_emit_planner_warning_group,
                api_key_override=None,
                render_reply=lambda message, questions: _render_planner_reply(
                    console=console,
                    message=message,
                    questions=questions,
                ),
                selection_label="planner",
                planning_relevant=True,
            )
            continue

        if cmd == "/goal":
            if not arg:
                console.print("[yellow]Usage:[/yellow] /goal <text>")
                append_transcript_note(paths, role="system", message="Rejected empty /goal.")
                continue
            goal = arg
            plan["project_goal"] = goal
            if not str(plan.get("summary") or "").strip():
                plan["summary"] = goal
            save_plan(paths, plan)
            console.print("Project goal updated.")
            if "goal" not in planning_suggested:
                planning_suggested.add("goal")
                console.print(
                    "[dim]Next: add tasks with /task <title>, or describe the work and "
                    "let the planner draft them.[/dim]"
                )
            append_transcript_note(paths, role="system", message="Updated project goal.")
            continue
        if cmd == "/task":
            if not arg:
                console.print("[yellow]Usage:[/yellow] /task <title>")
                append_transcript_note(paths, role="system", message="Rejected empty /task.")
                continue
            title = arg
            try:
                task = add_task(
                    plan,
                    title=title,
                    description=f"Manual planning chat task: {title}",
                )
            except ForgeError as e:
                console.print(f"[yellow]Task rejected:[/yellow] {e}")
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Rejected /task because it lacked runnable file scope: {e}",
                )
                continue
            save_plan(paths, plan)
            console.print(f"Added task: {task['id']} - {task['title']}")
            if "task" not in planning_suggested:
                planning_suggested.add("task")
                console.print(
                    "[dim]Next: add more tasks, or /done to save and validate the plan.[/dim]"
                )
            append_transcript_note(paths, role="system", message=f"Added task {task['id']}.")
            continue

        add_requirement(plan, text)
        save_plan(paths, plan)
        console.print("Captured requirement note.")
        append_transcript_note(paths, role="system", message="Captured requirement note.")

    finalize_plan(plan)
    reconciliation_result, _ = _reconcile_plan_for_paths(
        paths=paths,
        plan=plan,
        refresh_if_stale=True,
        transcript_tail=planner_state.transcript,
    )
    save_plan(paths, plan)
    validation_warnings = _validate_forge_plan_for_paths(paths, plan)
    if reconciliation_result.warnings:
        console.print("[yellow]Plan reconciliation warnings:[/yellow]")
        for warning in reconciliation_result.warnings:
            console.print(f"- {warning}")
            append_transcript_note(
                paths,
                role="system",
                message=f"Plan reconciliation warning: {warning}",
            )
    _write_plan_validation_artifact(
        paths=paths,
        reconciliation_result=reconciliation_result,
        validation_warnings=validation_warnings,
    )
    if validation_warnings:
        console.print("[yellow]Plan validation warnings:[/yellow]")
        for warning in validation_warnings:
            console.print(f"- {warning}")
            append_transcript_note(
                paths,
                role="system",
                message=f"Plan validation warning: {warning}",
            )

    # Stamp the plan with an explicit status so a plan that only earned warnings
    # is stored as a draft rather than looking ready until exec rejects it.
    status_assessment = apply_plan_status(plan, validation_warnings=validation_warnings)
    save_plan(paths, plan)
    if status_assessment.status == PLAN_STATUS_DRAFT:
        console.print("[yellow]Plan status:[/yellow] draft (not execution-ready)")
        for reason in status_assessment.blocking_reasons[:5]:
            console.print(f"- {reason}")
        append_transcript_note(
            paths,
            role="system",
            message="Plan saved as draft: " + "; ".join(status_assessment.blocking_reasons[:5]),
        )
    else:
        console.print("Plan status: execution_ready")

    console.print(f"Plan saved: {paths.plan_md_path}")
    console.print(f"Structured plan: {paths.plan_json_path}")

    tasks = plan.get("tasks") or []
    not_ready_reason = _plan_execution_readiness(plan)
    repair_payload = plan_repair_event_payload(plan)
    plan_saved_payload = {
        "run_id": paths.run_id,
        "plan_md": os.fspath(paths.plan_md_path),
        "plan_json": os.fspath(paths.plan_json_path),
        "run_dir": os.fspath(paths.run_dir),
        "project_goal": str(plan.get("project_goal") or "").strip() or None,
        "task_count": len(tasks),
        "task_ids": [str(task.get("id", "")) for task in tasks],
        "execution_ready": not_ready_reason is None,
        "warnings": list(validation_warnings),
        "reconciliation_warnings": list(reconciliation_result.warnings),
        **repair_payload,
    }
    events.emit(EVENT_PLAN_SAVED, plan_saved_payload)
    if not_ready_reason is not None:
        events.emit(
            EVENT_PLAN_INVALID,
            {
                "reason": not_ready_reason,
                "source": "execution_readiness",
                "warnings": list(validation_warnings),
                **repair_payload,
            },
        )
    events.run_completed(ok=True, exit_code=EXIT_OK, data={"plan": plan_saved_payload})


_SEQUENTIAL_DELEGATE_ENV = "ALYSIS_SWARM_SEQUENTIAL_DELEGATE"


def _swarm_sequential_delegation_blocker(
    *,
    cfg: Any,
    parallel: int,
    dry_run: Any,
    keep_worktrees: Any,
    retry_merge_conflicts: Any,
    replan: str | None,
    integration_verify: str | None,
    integration_verify_cmd: list[str] | None,
) -> str | None:
    """Why ``--parallel 1`` must stay on the swarm engine, or None to delegate.

    Delegation is only safe when nothing swarm-specific was requested. Every
    blocker below names a capability the sequential engine genuinely does not
    have, so the answer is always "keep the swarm", never "quietly drop it".
    """
    if not isinstance(parallel, int) or parallel != 1:
        return "parallel > 1"
    if str(env_get(_SEQUENTIAL_DELEGATE_ENV, "")).strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return f"{_SEQUENTIAL_DELEGATE_ENV} disables delegation"
    if dry_run is True:
        return "--dry-run reports the swarm schedule"
    if keep_worktrees is True:
        return "--keep-worktrees is worktree-specific"
    if retry_merge_conflicts is True:
        return "--retry-merge-conflicts re-merges existing task branches"
    if integration_verify is not None or integration_verify_cmd:
        return "an explicit integration verification gate was requested"
    try:
        replanning_mode = resolve_replanning_mode(cfg=cfg, replanning_mode=replan)
    except Exception:  # noqa: BLE001
        # Failing to classify replanning must not decide the engine; keep the
        # swarm, which is what the caller literally typed.
        return "between-batch replanning mode could not be resolved"
    if str(replanning_mode) != "off":
        return f"between-batch replanning is {replanning_mode}"
    return None


def _load_swarm_summary_payload(paths: Any) -> dict[str, Any] | None:
    """Read the structured swarm outcome the orchestrator already writes."""
    summary_path = paths.execution_dir / "swarm_summary.json"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _emit_swarm_outcome_events(
    *,
    events: ForgeEventEmitter,
    paths: Any,
    exit_code: int,
    dry_run: bool,
) -> None:
    """Translate the swarm's own outcome artifact into protocol events."""
    if not events.enabled:
        return

    summary = _load_swarm_summary_payload(paths)
    data: dict[str, Any] = {"dry_run": bool(dry_run)}
    if summary is not None:
        verification_status = str(summary.get("verification_status") or "")
        integration = summary.get("integration")
        integration_payload = integration if isinstance(integration, dict) else {}
        if verification_status == "not_run":
            events.emit(
                EVENT_VERIFICATION_UNAVAILABLE,
                {
                    "scope": "swarm",
                    "status": verification_status,
                    "reason": "no integration verification ran for this swarm run",
                },
            )
        elif verification_status:
            events.emit(
                EVENT_VERIFICATION_RESULT,
                {
                    "scope": "swarm",
                    "status": verification_status,
                    "passed": verification_status == "passed",
                    "integration": integration_payload,
                },
            )
        data["outcome"] = summary
    elif not dry_run:
        events.emit(
            EVENT_VERIFICATION_UNAVAILABLE,
            {
                "scope": "swarm",
                "status": "unknown",
                "reason": "swarm summary artifact was not written",
            },
        )

    events.run_completed(ok=exit_code == EXIT_OK, exit_code=exit_code, data=data)


def forge_swarm(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of requiring a narrower project path.",
    ),
    parallel: int = typer.Option(2, "--parallel", min=1, help="Parallel workers per batch."),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch (defaults to current checked out branch).",
    ),
    max_tasks: int | None = typer.Option(
        None,
        "--max-tasks",
        min=1,
        help="Maximum number of tasks to execute in this swarm run.",
    ),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        min=1,
        help="Maximum attempts allowed per task before scheduler skips it.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print schedule and exit."),
    keep_worktrees: bool = typer.Option(
        False,
        "--keep-worktrees",
        help=(
            "Keep failed task worktrees and branches for debugging; default cleanup removes "
            "successful worktrees and rejected failed branch state."
        ),
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Include tasks currently marked failed.",
    ),
    retry_changes_requested: bool = typer.Option(
        False,
        "--retry-changes-requested",
        help="Include tasks currently marked changes_requested.",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated task ids to execute (still enforces dependencies).",
    ),
    retry_merge_conflicts: bool = typer.Option(
        False,
        "--retry-merge-conflicts",
        help="Retry tasks marked merge_conflict during merge phase.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Verification policy: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    integration_verify: str | None = typer.Option(
        None,
        "--integration-verify",
        help="Batch integration verification policy: off, warn, or strict (defaults to config: warn).",
    ),
    integration_verify_cmd: list[str] | None = typer.Option(
        None,
        "--integration-verify-cmd",
        help="Override integration verify command for this swarm run (repeatable).",
    ),
    replan: str | None = typer.Option(
        None,
        "--replan",
        help="Between-batch replanning mode: off, suggest, or apply.",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run automated PR review gate before merging task branches.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    cli_ctx: Any = None,
    events: Any = None,
) -> None:
    events = _events_or_null(events)
    console = _console()
    cfg = load_config()

    # A swarm of one is a sequential run wearing worktrees. Hand it to the sequential
    # engine so the blessed path is what actually executes, unless the invocation asked
    # for machinery only the swarm has.
    delegation_blocker = _swarm_sequential_delegation_blocker(
        cfg=cfg,
        parallel=parallel,
        dry_run=dry_run,
        keep_worktrees=keep_worktrees,
        retry_merge_conflicts=retry_merge_conflicts,
        replan=replan,
        integration_verify=integration_verify,
        integration_verify_cmd=integration_verify_cmd,
    )
    if isinstance(parallel, int) and parallel == 1:
        if delegation_blocker is None:
            console.print(
                "[dim]forge swarm --parallel 1 has no parallelism to exploit; running the "
                "sequential engine (`forge run`) in this checkout instead of provisioning "
                "worktrees.[/dim]"
            )
            return forge_run(
                path=path,
                allow_broad_workspace=allow_broad_workspace,
                only=only,
                max_tasks=max_tasks,
                max_attempts=max_attempts,
                retry_failed=retry_failed,
                retry_changes_requested=retry_changes_requested,
                keep_going=False,
                dry_run=False,
                scope=scope,
                verify=verify,
                verify_cmd=verify_cmd,
                verify_repair_attempts=None,
                pr=True,
                review=review,
                base_branch=base_branch,
                keep_branch=False,
                auto_resolve_conflicts=False,
                mode=mode,
                model=model,
                base_url=base_url,
                temperature=temperature,
                stream=stream,
                max_steps=max_steps,
                no_log=no_log,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
                api_key=api_key,
                yes=yes,
                cli_ctx=cli_ctx,
                events=events,
            )
        console.print(
            f"[dim]forge swarm --parallel 1 kept the swarm engine: {delegation_blocker}. "
            "Use `forge run` for the sequential path.[/dim]"
        )

    effective = clone_cfg(cfg)
    current_ctx = get_current_context(silent=True)
    max_steps_source = (
        current_ctx.get_parameter_source("max_steps") if current_ctx is not None else None
    )
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        max_steps_provided = (
            max_steps_source is not None and max_steps_source is not ParameterSource.DEFAULT
        )
    if base_url is not None:
        effective.base_url = base_url
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    if stream is not None:
        effective.stream = stream
    if max_steps is not None:
        effective.max_steps = max_steps
    swarm_max_steps = effective.max_steps if max_steps_provided else None

    effective_mode = (mode.value if mode else effective.default_mode) or "review"
    scope_mode = "strict"
    verify_mode = "warn"
    integration_verify_mode = None
    replanning_mode = None

    try:
        scope_mode = _normalize_scope_mode(scope)
        verify_mode = _normalize_verify_mode(verify)
        integration_verify_mode = integration_verify
        replanning_mode = replan
        api_key_override = _resolve_api_key_override(
            api_key=api_key,
            api_key_env=api_key_env,
            api_key_stdin=api_key_stdin,
        )
        binding = resolve_workspace_binding(
            path,
            create_if_missing=False,
            allow_broad_workspace=allow_broad_workspace,
            source=_path_binding_source(cli_ctx),
        )
        ensure_workspace_policy(
            binding,
            action=WorkspaceAction.SWARM,
            allow_broad_workspace=allow_broad_workspace,
        )
        try:
            paths = load_current_run_paths(binding.workspace_context.focus_path)
        except ForgeError as e:
            if "current_run.json" in str(e):
                raise _missing_swarm_run_error(binding=binding) from e
            raise
        events.set_run_id(paths.run_id)
        plan = load_plan(paths)
        run_mutation_guard = acquire_swarm_mutation_guard(
            paths,
            mode="forge_swarm:cli",
            on_wait=lambda info: _print_forge_lock_wait_notice(console, info),
        )
        try:
            if bool(getattr(run_mutation_guard, "acquired_after_wait", False)):
                plan = load_plan(paths)
            reconciliation_result, _ = _reconcile_plan_for_paths(
                paths=paths,
                plan=plan,
                refresh_if_stale=True,
            )
            if reconciliation_result.changed:
                save_plan(paths, plan)
            if reconciliation_result.warnings:
                console.print("[yellow]Plan reconciliation warnings:[/yellow]")
                for warning in reconciliation_result.warnings:
                    console.print(f"- {warning}")
            validation_warnings = _validate_forge_plan_for_paths(paths, plan)
            _write_plan_validation_artifact(
                paths=paths,
                reconciliation_result=reconciliation_result,
                validation_warnings=validation_warnings,
            )
            repair_payload = plan_repair_event_payload(plan)
            no_execution_ready_tasks_message = _forge_no_execution_ready_tasks_message(plan)
            if no_execution_ready_tasks_message is not None:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": no_execution_ready_tasks_message,
                        "source": "execution_readiness",
                        "warnings": list(validation_warnings),
                        **repair_payload,
                    },
                )
                raise ForgeError(no_execution_ready_tasks_message)
            try:
                raise_for_execution_ready_plan(
                    plan,
                    retry_failed=retry_failed,
                    retry_changes_requested=retry_changes_requested,
                    retry_merge_conflicts=retry_merge_conflicts,
                    only=only,
                )
            except PlannerFailedError as e:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": str(e),
                        "source": "plan_validation",
                        "failure_category": getattr(e, "failure_category", None),
                        **repair_payload,
                    },
                )
                err = ForgeError(str(e))
                err.failure_category = e.failure_category  # type: ignore[attr-defined]
                raise err from e
            resolve_model_for_role(
                cfg=effective,
                role=ROLE_CODING,
                plan=plan,
                prefer_context="forge",
            )
            if not dry_run:
                _mark_run_status(
                    paths,
                    RUN_STATUS_RUNNING,
                    reason=f"forge swarm started (parallel={parallel})",
                    plan=plan,
                    mode="forge_swarm:cli",
                )
            code = run_swarm(
                paths=paths,
                plan=plan,
                cfg=effective,
                mode=effective_mode,
                yes=yes,
                max_steps=swarm_max_steps,
                api_key_override=api_key_override,
                no_log=no_log,
                parallel=parallel,
                base_branch=base_branch,
                max_tasks=max_tasks,
                max_attempts=max_attempts,
                dry_run=dry_run,
                keep_worktrees=keep_worktrees,
                retry_failed=retry_failed,
                retry_changes_requested=retry_changes_requested,
                only=only,
                retry_merge_conflicts=retry_merge_conflicts,
                scope_mode=scope_mode,
                verify_mode=verify_mode,
                verify_cmd=verify_cmd,
                integration_mode=integration_verify_mode,
                integration_verify_cmd=integration_verify_cmd,
                replanning_mode=replanning_mode,
                review=review,
                console=console,
                workspace_binding=binding,
                run_mutation_guard=run_mutation_guard,
            )
            if not dry_run:
                # The swarm reloads and rewrites the plan as it merges batches, so the
                # in-memory copy is stale by now; the terminal status has to be decided
                # from what actually landed on disk.
                terminal_status, terminal_reason = _terminal_run_status_for(
                    plan=load_plan(paths),
                    any_failure=code != EXIT_OK,
                )
                _mark_run_status(paths, terminal_status, reason=terminal_reason)
        finally:
            _mark_run_interrupted_if_still_running(
                paths,
                reason="forge swarm exited without recording an outcome",
            )
            run_mutation_guard.release()
    except (ConfigError, ForgeError, GitOpsError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e),
            kind="forge_error",
            exit_code=EXIT_ERROR,
            data={"failure_category": getattr(e, "failure_category", None)},
        )
        raise typer.Exit(code=EXIT_ERROR) from e
    except Exception as e:  # noqa: BLE001
        # An unexpected exception is a command error, not "the swarm ran and some tasks
        # failed" -- exit 2 keeps exit 1 meaning only the latter.
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e) or e.__class__.__name__,
            kind="exception",
            exit_code=EXIT_ERROR,
            data={"exception_type": e.__class__.__name__},
        )
        raise typer.Exit(code=EXIT_ERROR) from e

    log_paths = sorted(paths.execution_logs_dir.glob("*.jsonl"))
    _print_usage_summary_from_logs(
        console=console,
        title="Swarm Usage Summary",
        log_paths=log_paths,
    )
    _emit_swarm_outcome_events(events=events, paths=paths, exit_code=code, dry_run=dry_run)
    raise typer.Exit(code=code)


def execute_forge_task(
    *,
    console: Any,
    events: ForgeEventEmitter,
    paths: Any,
    plan: dict[str, Any],
    task: dict[str, Any],
    task_id: str,
    effective: Any,
    run_cfg: Any,
    effective_mode: str,
    scope_mode: str,
    verify_mode: str,
    verify_commands: list[str],
    verify_command_source: str | None,
    verify_repair_budget: int,
    api_key_override: str | None,
    yes: bool,
    no_log: bool,
    max_steps_provided: bool,
    pr: bool,
    review: bool,
    base_branch: str | None,
    keep_branch: bool,
    auto_resolve_conflicts: bool = False,
) -> TaskExecutionOutcome:
    """Execute exactly one plan task in the main checkout, with no worktrees.

    This is the sequential execution core. ``forge exec`` calls it once and turns
    the outcome into an exit code; ``forge run`` calls it per ready task in
    dependency order. Sharing it is the point: scope triage, verification, the
    repair loop, the review gate and the PR flow behave identically either way,
    so a fix to one is a fix to both.

    The caller owns the run mutation guard, the plan reload and the terminal
    event. Raising :class:`ForgeTaskExecutionError` means the task could not be
    executed at all (a command error); returning an outcome with
    ``success=False`` means it ran and was not accepted.
    """
    pr_base_branch: str | None = None
    pr_task_branch: str | None = None
    commit_hash: str | None = None
    merge_commit_hash: str | None = None
    merge_result: str | None = None
    allowed_scope = normalize_scope_patterns(task, root=paths.root) if scope_mode != "off" else []
    scope_warnings: list[str] = []
    review_blocked = False
    verify_blocked = False
    # Set when the work landed but strict verification had no authoritative
    # command to hold it against: a completion, not a failure, and never merged.
    verification_unavailable_completion = False
    verification_repair_payload: dict[str, Any] | None = None
    verify_summary: str | None = None
    verify_payload: dict[str, Any] | None = None
    merge_conflict_detected = False
    conflict_review_path: Path | None = None
    verify_path = paths.execution_verify_dir / f"{_safe_task_file_component(task_id)}.txt"
    remote_settings = load_remote_settings_from_env()
    remote_record: dict[str, Any] | None = None
    conflict_auto_settings = load_conflict_auto_resolve_settings(cfg=effective)

    if pr:
        try:
            ensure_git_available()
            ensure_git_repo(paths.root)
            ensure_clean_for_pr(paths.root)
            pr_base_branch = base_branch.strip() if base_branch else current_branch(paths.root)
            if not pr_base_branch:
                raise GitOpsError("base branch is empty")
            pr_task_branch = str(task.get("branch") or "").strip()
            if not pr_task_branch:
                pr_task_branch = generate_task_branch_name(
                    task_id,
                    str(task.get("title") or ""),
                )
                task["branch"] = pr_task_branch
                save_plan(paths, plan)
            checkout_branch(paths.root, pr_task_branch, base_branch=pr_base_branch)
            merge_result = "not merged"
        except GitOpsError as e:
            raise ForgeTaskExecutionError(
                str(e),
                kind="git_error",
                task_id=task_id,
            ) from e

    ensure_execution_dirs(paths)
    set_task_status(plan, task_id, "in_progress")
    save_plan(paths, plan)
    events.emit(
        EVENT_TASK_STARTED,
        {
            "task_id": task_id,
            "title": str(task.get("title", "")),
            "status": "in_progress",
            "source": "exec",
            "pr": bool(pr),
            "branch": pr_task_branch,
        },
    )

    started_at = now_iso()
    run_non_interactive = _is_non_interactive_terminal()
    prompt_verification_enabled = verify_mode != "off" and bool(verify_commands)
    prepared_knowledge = _prepare_task_execution_knowledge(
        run_paths=paths,
        task=task,
        selection_label="execution",
    )
    runtime_session_id = _safe_task_file_component(task_id)
    task_mcp_scope, task_mcp_scope_warnings = normalize_task_mcp_scope(
        task.get("mcp_scope"),
        warning_prefix=f"Task {task_id}",
    )
    scope_warnings.extend(task_mcp_scope_warnings)
    instruction = ""
    task_image_paths: list[str] | None = None
    budget_artifact_path = paths.execution_budgets_dir / f"{runtime_session_id}.json"
    runtime_sessions_dir = _execution_private_sessions_dir(
        cfg=run_cfg,
        run_id=paths.run_id,
        task_id=task_id,
        workspace_root=paths.root,
    )
    _cleanup_execution_private_sessions_dir(runtime_sessions_dir)
    task_attempts_raw = task.get("attempts")
    try:
        task_attempt_count = (
            max(0, int(task_attempts_raw if task_attempts_raw is not None else 0)) + 1
        )
    except (TypeError, ValueError):
        task_attempt_count = 1
    task_step_budget = _resolve_managed_task_step_budget(
        cfg=run_cfg,
        plan=plan,
        task=task,
        kind="managed_task",
        mode=effective_mode,
        verification_enabled=verify_mode != "off",
        max_steps_override=(effective.max_steps if max_steps_provided else None),
        attempt_count=task_attempt_count,
        image_count=0,
    )
    head_before_run = head_commit(paths.root) if paths.has_head_commit else None
    before_runtime_snapshot: dict[str, str] | None = None
    reporting_baseline: Any | None = None
    recording_surface = RecordingSurface(_make_rich_surface(console=console))
    asset_setup_error: str | None = None
    asset_setup_warnings: list[str] = []
    asset_usage_logger = AssetUsageLogger(run_paths=paths, task_id=task_id)
    asset_model_registry = ModelRegistry(cfg=run_cfg)
    asset_surface = (
        build_asset_surface(
            cfg=run_cfg,
            run_paths=paths,
            model_registry=asset_model_registry,
        )
        if run_cfg.assets.enabled
        else None
    )
    task_asset_mirror = _empty_forge_exec_task_asset_mirror(paths.root, task_id=task_id)
    if asset_surface is not None:
        try:
            task_asset_mirror = mirror_task_assets(
                task=task,
                plan=plan,
                surface=asset_surface,
                workspace_path=paths.root,
            )
        except AssetError as exc:
            if run_cfg.assets.worker.fail_on_mirror_error:
                asset_setup_error = (
                    f"forge exec asset mirror failed: {sanitize_error_summary(str(exc))}"
                )
            else:
                asset_setup_warnings.append(
                    f"forge exec asset mirror skipped: {sanitize_optional_error_summary(str(exc))}"
                )
    scope_warnings.extend(asset_setup_warnings)
    for entry in [
        *task_asset_mirror.primary,
        *task_asset_mirror.may_need,
        *task_asset_mirror.pinned,
    ]:
        asset_usage_logger.mirror(
            asset_id=entry.asset_id,
            kind=entry.kind,
            status=entry.status,
        )
    has_mirrored_task_assets = bool(
        task_asset_mirror.primary or task_asset_mirror.may_need or task_asset_mirror.pinned
    )

    run_code = 1
    run_err: str | None = asset_setup_error
    task_mcp_manager: Any | None = None
    asset_allocation: TaskAssetAllocation | None = None
    try:
        task_mcp_manager = _build_forge_task_scoped_mcp_manager(
            workspace_root=paths.root,
            session_id=runtime_session_id,
            task_scope=task_mcp_scope,
        )
        mcp_context_section = _build_forge_mcp_execution_context_section(
            task_scope=task_mcp_scope,
            mcp_manager=task_mcp_manager,
        )
        instruction_bundle = _build_forge_exec_instruction_bundle(
            plan=plan,
            task=task,
            root=paths.root,
            cfg=run_cfg,
            role_model=run_cfg.model,
            mode=effective_mode,
            yes=yes,
            deny_write_prefixes=[".alysis"],
            allow_write_globs=allowed_scope if scope_mode == "strict" else None,
            non_interactive=run_non_interactive,
            verification_enabled=prompt_verification_enabled,
            authoritative_verification_commands=(
                verify_commands if prompt_verification_enabled else None
            ),
            api_key=api_key_override,
            subagents_enabled=False,
            leading_sections=[prepared_knowledge.prompt_section, mcp_context_section],
        )
        relevant_assets_section = ""
        if asset_surface is not None and task_asset_mirror.primary:
            asset_allocation = allocate_task_assets(
                task=task,
                plan=plan,
                mirror=task_asset_mirror,
                cfg=run_cfg,
                model_registry=asset_model_registry,
                instruction_token_budget=instruction_bundle.budget.final_instruction_budget,
                api_key=api_key_override,
            )
            for decision in asset_allocation.decisions:
                asset_usage_logger.allocation_decision(
                    asset_id=decision.asset_id,
                    mode=decision.mode,
                )
            relevant_assets_section = render_relevant_assets_section(
                mirror=task_asset_mirror,
                allocation=asset_allocation,
                cfg=run_cfg,
                surface=asset_surface,
                model_registry=asset_model_registry,
                api_key=api_key_override,
            )
        elif asset_surface is not None and (task_asset_mirror.may_need or task_asset_mirror.pinned):
            asset_allocation = TaskAssetAllocation(
                task_id=task_id,
                decisions=[],
                elapsed_ms=0,
                model=None,
                tokens_used={},
                fallback_used=False,
                fallback_reason=None,
            )
            relevant_assets_section = render_relevant_assets_section(
                mirror=task_asset_mirror,
                allocation=asset_allocation,
                cfg=run_cfg,
                surface=asset_surface,
                model_registry=asset_model_registry,
                api_key=api_key_override,
            )
        if relevant_assets_section:
            instruction_bundle = _build_forge_exec_instruction_bundle(
                plan=plan,
                task=task,
                root=paths.root,
                cfg=run_cfg,
                role_model=run_cfg.model,
                mode=effective_mode,
                yes=yes,
                deny_write_prefixes=[".alysis"],
                allow_write_globs=allowed_scope if scope_mode == "strict" else None,
                non_interactive=run_non_interactive,
                verification_enabled=prompt_verification_enabled,
                authoritative_verification_commands=(
                    verify_commands if prompt_verification_enabled else None
                ),
                api_key=api_key_override,
                subagents_enabled=False,
                leading_sections=[prepared_knowledge.prompt_section, mcp_context_section],
                relevant_assets_section=relevant_assets_section,
            )
        instruction = instruction_bundle.instruction
        _write_execution_context_artifact(
            paths=paths,
            task_id=task_id,
            context_text=instruction_bundle.artifact_text,
        )
        task_image_paths = _combined_forge_exec_image_paths(
            legacy_paths=list(instruction_bundle.image_paths),
            mirror=task_asset_mirror,
            allocation=asset_allocation,
            cfg=run_cfg,
            role_model=run_cfg.model,
            model_registry=asset_model_registry,
            usage_logger=asset_usage_logger,
        )
        task_step_budget = _resolve_managed_task_step_budget(
            cfg=run_cfg,
            plan=plan,
            task=task,
            kind="managed_task",
            mode=effective_mode,
            verification_enabled=verify_mode != "off",
            max_steps_override=(effective.max_steps if max_steps_provided else None),
            attempt_count=task_attempt_count,
            image_count=len(task_image_paths or []),
        )
        budget_artifact_payload = instruction_bundle.to_budget_artifact_payload()
        budget_artifact_payload["step_budget"] = task_step_budget.to_payload()
        budget_artifact_path = _write_execution_budget_artifact(
            paths=paths,
            task_id=task_id,
            payload=budget_artifact_payload,
        )
        before_runtime_snapshot = _snapshot_runtime_tree(paths.root)
        reporting_baseline = _capture_task_local_workspace_baseline(
            paths.root,
            before_commit=head_before_run,
        )
        if run_err is None:
            if asset_surface is not None and has_mirrored_task_assets:
                task_mcp_manager = compose_worker_asset_mcp_manager(
                    base_manager=task_mcp_manager,
                    asset_manager=build_worker_asset_mcp_manager(
                        cfg=run_cfg,
                        surface=asset_surface,
                        model_registry=asset_model_registry,
                        mirror=task_asset_mirror,
                        usage_logger=asset_usage_logger,
                        api_key=api_key_override,
                    ),
                )
            run_code = run_agent(
                cfg=run_cfg,
                root=paths.root,
                instruction=instruction,
                mode=effective_mode,
                runtime_kind=RuntimeKind.FORGE_EXEC,
                yes=yes,
                max_steps=task_step_budget.resolved_max_steps,
                no_log=no_log,
                api_key_override=api_key_override,
                console=console,
                surface=recording_surface,
                image_paths=task_image_paths,
                deny_write_prefixes=[".alysis"],
                allow_write_globs=allowed_scope if scope_mode == "strict" else None,
                non_interactive=run_non_interactive,
                session_log_dir_override=runtime_sessions_dir,
                session_id_override=runtime_session_id,
                usage_role=f"forge_exec:{task_id}",
                enable_compaction=False,
                enable_tool_output_offload=True,
                enable_conversation_summarization=True,
                compaction_profile="execution",
                enable_chat_turn_step_budget=False,
                one_shot_execution=True,
                verification_enabled=prompt_verification_enabled,
                authoritative_verification_commands=(
                    verify_commands if prompt_verification_enabled else None
                ),
                subagents_enabled=False,
                enforce_explicit_subagent_requests=False,
                mcp_manager=task_mcp_manager,
                session_source_metadata={
                    "surface": "forge_exec",
                    "run_id": paths.run_id,
                    "task_id": str(task_id),
                },
            )
    except Exception as e:  # noqa: BLE001
        run_code = 1
        run_err = str(e)
        if not instruction:
            _write_execution_context_artifact(
                paths=paths,
                task_id=task_id,
                context_text=(
                    "# Task Context Pack\n\n"
                    "Task execution setup failed before the agent started.\n\n"
                    f"- Error: {run_err}\n"
                ),
            )
        if not budget_artifact_path.exists():
            _write_execution_budget_artifact(
                paths=paths,
                task_id=task_id,
                payload={
                    "error": run_err,
                    "step_budget": task_step_budget.to_payload(),
                },
            )
        if before_runtime_snapshot is None:
            before_runtime_snapshot = _snapshot_runtime_tree(paths.root)
        if reporting_baseline is None:
            reporting_baseline = _capture_task_local_workspace_baseline(
                paths.root,
                before_commit=head_before_run,
            )
    finally:
        if task_mcp_manager is not None:
            task_mcp_manager.close()

    assert before_runtime_snapshot is not None
    assert reporting_baseline is not None
    after_runtime_snapshot = _snapshot_runtime_tree(paths.root)
    runtime_artifact_changes = _runtime_snapshot_changed_files(
        before_runtime_snapshot,
        after_runtime_snapshot,
    )
    authorized_runtime_side_effects = _authorized_custom_tool_runtime_side_effects(
        sessions_dir=runtime_sessions_dir,
        session_id=runtime_session_id,
    )
    runtime_artifact_changes = [
        path for path in runtime_artifact_changes if path not in authorized_runtime_side_effects
    ]
    runtime_artifacts_changed = bool(runtime_artifact_changes)
    if asset_allocation is not None:
        write_task_asset_allocation(
            run_paths=paths,
            allocation=asset_allocation,
            started_at=started_at,
        )
    asset_usage_logger.summary(
        primary_count=len(task_asset_mirror.primary),
        may_need_count=len(task_asset_mirror.may_need),
        pinned_count=len(task_asset_mirror.pinned),
    )
    try:
        exec_artifacts = _write_exec_log_artifacts(
            paths=paths,
            task_id=task_id,
            cfg=run_cfg,
            no_log=no_log,
            before_logs=None,
            sessions_dir=runtime_sessions_dir,
            expected_session_id=runtime_session_id,
        )
    finally:
        _cleanup_execution_private_sessions_dir(runtime_sessions_dir)

    safe_task_component = _safe_task_file_component(task_id)
    patch_path = paths.execution_patches_dir / f"{safe_task_component}.diff"
    scratch_artifact_dir = paths.execution_dir / "scratch" / safe_task_component
    scratch_artifact_dir.mkdir(parents=True, exist_ok=True)
    success = run_code == 0 and not runtime_artifacts_changed
    pr_report_state_upgraded = False
    head_after_run = head_commit(paths.root) if paths.has_head_commit else None
    # Captured before cleanup so scope triage can tell a file the task created from an
    # existing neighbour it merely edited.
    baseline_workspace_paths = set(reporting_baseline.before_snapshot)
    try:
        report_diff = _build_task_local_workspace_reporting_diff(
            paths.root,
            baseline=reporting_baseline,
            after_commit=head_after_run,
        )
    finally:
        _cleanup_task_local_workspace_baseline(reporting_baseline)
    patch_path.write_text(report_diff.patch_text, encoding="utf-8")
    scratch_scope_diagnostics = relocate_known_scratch_artifacts(
        root=paths.root,
        artifact_dir=scratch_artifact_dir,
    )
    relocated_scratch_paths = {item.path for item in scratch_scope_diagnostics}
    changed_files = list(report_diff.changed_files)
    if relocated_scratch_paths:
        changed_files = [path for path in changed_files if path not in relocated_scratch_paths]
    agent_added_non_material_paths: list[str] = []
    if head_before_run and head_after_run and head_after_run != head_before_run:
        agent_added_non_material_paths = [
            path
            for path in added_files_since(
                paths.root,
                before_commit=head_before_run,
                after_commit=head_after_run,
            )
            if is_non_material_untracked_path(path)
        ]
        if agent_added_non_material_paths:
            changed_files = [
                path for path in changed_files if path not in agent_added_non_material_paths
            ]
    pr_material_changed_files = (
        _drop_parent_directory_placeholders(
            _merge_changed_files(
                list(changed_files),
                list_changed_files_including_untracked(paths.root),
            )
        )
        if pr
        else list(changed_files)
    )
    scope_changed_files = pr_material_changed_files if pr else changed_files
    scope_inspection_error = report_diff.inspection_error
    scope_violation_files: list[str] = []
    scope_diagnostics = [item.to_payload() for item in scratch_scope_diagnostics]
    for diagnostic in scratch_scope_diagnostics:
        scope_warnings.append(
            "Scope recovery: "
            f"{diagnostic.classification} for {diagnostic.path} "
            f"({diagnostic.reason_code}; action={diagnostic.recommended_action})."
        )
    material_changes_detected = bool(pr_material_changed_files)
    nonzero_agent_exit = run_code != 0 and run_err is None
    scope_amendment_payloads: list[dict[str, Any]] = []
    scope_amended_patterns: list[str] = []
    scope_in_scope_changes = False
    adjacent_only_changes = False
    strict_scope_blocked = False
    can_attempt_pr_flow = False
    pr_nonzero_salvage_allowed = False
    pr_nonzero_salvage_attempted = False
    no_material_changes_blocked = False
    result_kind: str | None = None
    noop_reason: str | None = None
    analysis_only_noop_accepted = False
    if scope_mode in {"warn", "strict"}:
        if scope_inspection_error:
            if scope_mode == "strict":
                strict_scope_blocked = True
                success = False
                run_err = (run_err + "; " if run_err else "") + scope_inspection_error
            else:
                scope_warnings.append(scope_inspection_error)
        # Adjacent changes are triaged into scope amendments only in strict mode: warn
        # mode has nothing to unblock, so amending there would silently rewrite the plan
        # for a mode whose whole contract is "report, change nothing".
        scope_assessment = assess_scope_changes(
            scope_changed_files,
            allowed_scope,
            task=task,
            root=paths.root,
            extra_diagnostics=scratch_scope_diagnostics,
            amend_adjacent=scope_mode == "strict",
            new_paths=[
                path
                for path in scope_changed_files
                if _normalize_changed_file_path(path) not in baseline_workspace_paths
            ],
        )
        scope_changed_files = list(scope_assessment.effective_changed_files)
        scope_diagnostics = [item.to_payload() for item in scope_assessment.diagnostics]
        scope_in_scope_changes = bool(scope_assessment.in_scope_paths)
        for diagnostic in scope_assessment.diagnostics:
            if diagnostic.allowed:
                warning = (
                    "Scope recovery: "
                    f"{diagnostic.classification} for {diagnostic.path} "
                    f"({diagnostic.reason_code}; action={diagnostic.recommended_action})."
                )
                if warning not in scope_warnings:
                    scope_warnings.append(warning)
        if scope_assessment.amendments:
            scope_amendment_payloads = [item.to_payload() for item in scope_assessment.amendments]
            scope_amended_patterns = apply_scope_amendments(
                task,
                scope_assessment.amendments,
            )
            adjacent_only_changes = not scope_in_scope_changes
            amendment_preview = ", ".join(
                f"{item.path} ({item.reason_code})" for item in scope_assessment.amendments[:20]
            )
            if len(scope_assessment.amendments) > 20:
                amendment_preview += ", ..."
            scope_warnings.append(
                f"Scope amended: {len(scope_assessment.amendments)} adjacent change(s) "
                f"accepted and added to write_scope: {amendment_preview}."
            )
            events.emit(
                EVENT_SCOPE_AMENDED,
                {
                    "task_id": task_id,
                    "scope_mode": scope_mode,
                    "allowed_scope": list(allowed_scope),
                    "added_patterns": list(scope_amended_patterns),
                    "amendments": scope_amendment_payloads,
                    "adjacent_paths": list(scope_assessment.adjacent_paths),
                    "adjacent_only": bool(adjacent_only_changes),
                },
            )
        if not scope_assessment.ok:
            violations = scope_assessment.blocking_paths
            scope_violation_files = list(violations)
            preview = ", ".join(violations[:20])
            if len(violations) > 20:
                preview += ", ..."
            classes = sorted(
                {
                    str(item.get("classification") or "unknown")
                    for item in scope_diagnostics
                    if not bool(item.get("allowed"))
                }
            )
            scope_msg = (
                f"Out-of-scope file changes detected ({len(violations)}): {preview}. "
                f"Allowed scope: {allowed_scope or ['(none)']}."
            )
            if classes:
                scope_msg += f" Scope classifications: {', '.join(classes)}."
            # The full triage plus a ready-to-paste scope patch, so a human or the
            # replanner can fix the plan in one step instead of re-deriving it.
            violation_lines = describe_scope_violations(scope_assessment.diagnostics)
            if violation_lines:
                scope_msg += " Classified changes: " + " | ".join(violation_lines) + "."
            if scope_assessment.protected_paths:
                scope_msg += (
                    " Protected paths (never amendable): "
                    + ", ".join(scope_assessment.protected_paths)
                    + "."
                )
            if scope_assessment.suggested_scope_patterns:
                scope_msg += (
                    " Suggested write_scope additions: "
                    + ", ".join(scope_assessment.suggested_scope_patterns)
                    + "."
                )
            if scope_mode == "strict":
                strict_scope_blocked = True
                success = False
                run_err = (
                    (run_err + "; " if run_err else "")
                    + scope_msg
                    + " Task was blocked due to strict scope isolation."
                )
            else:
                scope_warnings.append(scope_msg)

    if pr and not runtime_artifacts_changed and not material_changes_detected:
        success = False

    if run_code == 0 and not runtime_artifacts_changed and not material_changes_detected:
        if _task_is_analysis_only(task):
            if pr:
                if not pr_base_branch:
                    success = False
                    run_err = (run_err + "; " if run_err else "") + (
                        "missing PR base branch context for analysis-only no-op cleanup"
                    )
                    merge_result = "not merged: analysis-only no-op cleanup failed"
                else:
                    try:
                        checkout_branch(
                            paths.root,
                            pr_base_branch,
                            base_branch=pr_base_branch,
                        )
                        if keep_branch or not pr_task_branch or pr_task_branch == pr_base_branch:
                            merge_result = "no merge required (analysis-only no-op; branch kept)"
                        else:
                            try:
                                delete_branch(paths.root, pr_task_branch)
                                merge_result = (
                                    "no merge required (analysis-only no-op; branch deleted)"
                                )
                            except GitOpsError as cleanup_err:
                                scope_warnings.append(
                                    "Branch cleanup warning: "
                                    f"failed to delete {pr_task_branch}: {cleanup_err}"
                                )
                                merge_result = (
                                    "no merge required (analysis-only no-op; "
                                    f"branch delete warning: {cleanup_err})"
                                )
                        success = True
                    except GitOpsError as e:
                        success = False
                        run_err = (run_err + "; " if run_err else "") + (
                            f"PR no-op cleanup failed: {e}"
                        )
                        merge_result = f"not merged: analysis-only no-op cleanup failed: {e}"
            else:
                success = True
            if success:
                result_kind = "success_noop"
                noop_reason = "analysis_only"
                analysis_only_noop_accepted = True
                verify_summary = "verification skipped: analysis-only task made no changes"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                )
        elif _task_declares_explicit_write_scope(task):
            no_material_changes_blocked = True
            success = False
            run_err = (run_err + "; " if run_err else "") + (
                "No material file changes were detected for a task with explicit write_scope. "
                "The task produced no file changes at all -- not in the declared scope and "
                "not adjacent to it -- so its expected local file update was not produced."
            )
    elif (
        run_code == 0
        and not runtime_artifacts_changed
        and adjacent_only_changes
        and not strict_scope_blocked
    ):
        # No declared scope path changed, but the task did do work: every change was
        # adjacent and has been amended into write_scope. That is a pass with an
        # amendment, not the "task did nothing" rejection above.
        result_kind = result_kind or "success_scope_amended"
        scope_warnings.append(
            "No declared write_scope path changed; every change was adjacent to the "
            "declared scope and write_scope was amended to cover it."
        )

    pr_nonzero_salvage_allowed = (
        pr
        and nonzero_agent_exit
        and verify_mode == "strict"
        and bool(verify_commands)
        and not runtime_artifacts_changed
        and material_changes_detected
        and not strict_scope_blocked
    )

    if nonzero_agent_exit and pr_nonzero_salvage_allowed:
        pr_nonzero_salvage_attempted = True
        scope_warnings.append(
            f"PR flow attempted to salvage a non-zero agent exit ({run_code}); "
            "acceptance requires strict verification and PR gates."
        )
    elif nonzero_agent_exit:
        success = False
        run_err = (run_err + "; " if run_err else "") + (
            f"agent exited non-zero ({run_code}); refusing to accept partial task result"
        )

    can_attempt_pr_flow = (
        pr
        and not runtime_artifacts_changed
        and material_changes_detected
        and not strict_scope_blocked
        and (run_code == 0 or pr_nonzero_salvage_allowed)
    )

    if can_attempt_pr_flow:
        success = True
        if not pr_base_branch or not pr_task_branch:
            success = False
            run_err = (run_err + "; " if run_err else "") + "missing PR branch context"
            merge_result = "not merged"
        else:
            try:
                non_material_untracked_paths = list_untracked_packaging_metadata_paths(paths.root)
                stage_all(paths.root)
                unstage_staged_prefixes(
                    paths.root,
                    [".alysis", ".alysis_images", "alysis-feedback"],
                )
                ensure_not_staged_prefixes(
                    paths.root,
                    [".alysis", ".alysis_images", "alysis-feedback"],
                )
                if non_material_untracked_paths:
                    unstage_staged_paths(paths.root, non_material_untracked_paths)
                    ensure_not_staged_paths(paths.root, non_material_untracked_paths)
                if has_grounded_rust_target_runtime_artifacts(paths.root):
                    staged_now = staged_files(paths.root)
                    unstage_staged_runtime_artifacts(
                        paths.root,
                        current_paths=staged_now,
                    )
                    staged_now = staged_files(paths.root)
                    ensure_not_staged_runtime_artifacts(
                        paths.root,
                        current_paths=staged_now,
                    )
                commit_title = str(task.get("title") or "").strip() or "task update"
                commit_hash = commit_all(
                    paths.root,
                    message=f"{task_id}: {commit_title}",
                )
                patch_text = format_patch_stdout(paths.root, base_branch=pr_base_branch)
                patch_path.write_text(
                    patch_text if patch_text else "(empty format-patch output)\n",
                    encoding="utf-8",
                )
                changed_files = changed_files_between(
                    paths.root,
                    revspec=f"{pr_base_branch}..HEAD",
                )
                pr_report_state_upgraded = True
            except GitOpsError as e:
                success = False
                run_err = (run_err + "; " if run_err else "") + f"PR flow failed: {e}"
                merge_result = f"not merged: {e}"

            if success and remote_settings.enabled:
                if not pr_task_branch:
                    success = False
                    run_err = (
                        run_err + "; " if run_err else ""
                    ) + "missing task branch for remote sync"
                    merge_result = "not merged: remote sync branch context missing"
                else:
                    remote_name = remote_settings.remote_name
                    provider = "unknown"
                    remote_record = init_remote_record(
                        task_id=task_id,
                        remote=remote_name,
                        provider=provider,
                    )
                    remote_errors = remote_record["errors"]
                    assert isinstance(remote_errors, list)
                    try:
                        remote_url = get_remote_url(paths.root, remote_name)
                        provider = resolve_provider(
                            settings_provider=remote_settings.provider,
                            remote_url=remote_url,
                        )
                        remote_record["provider"] = provider
                    except RemoteSyncError as e:
                        msg = f"remote discovery failed: {e}"
                        remote_errors.append(msg)
                        if remote_settings.strict:
                            success = False
                            run_err = (run_err + "; " if run_err else "") + msg
                            merge_result = f"not merged: {msg}"
                        else:
                            scope_warnings.append(msg)

                    if success:
                        pushed_branch, branch_output = push_branch(
                            paths.root,
                            remote=remote_name,
                            branch=pr_task_branch,
                        )
                        remote_record["pushed_branch"] = pushed_branch
                        remote_record["branch_push_output"] = truncate_output(branch_output)
                        if not pushed_branch:
                            msg = f"remote branch push failed: {branch_output or 'unknown error'}"
                            remote_errors.append(msg)
                            if remote_settings.strict:
                                success = False
                                run_err = (run_err + "; " if run_err else "") + msg
                                merge_result = f"not merged: {msg}"
                            else:
                                scope_warnings.append(msg)

                    if success and remote_settings.create_pr and remote_record is not None:
                        created_pr, pr_url, pr_id, pr_output = ensure_pr_or_mr(
                            paths.root,
                            provider=str(remote_record.get("provider") or "unknown"),
                            base_branch=pr_base_branch,
                            head_branch=pr_task_branch,
                            title=(
                                f"{task_id}: "
                                f"{str(task.get('title') or '').strip() or 'task update'}"
                            ),
                            body=instruction[:4000],
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
                            remote_errors.append(msg)
                            if remote_settings.strict:
                                success = False
                                run_err = (run_err + "; " if run_err else "") + msg
                                merge_result = f"not merged: {msg}"
                            else:
                                scope_warnings.append(msg)

                    if remote_record is not None:
                        write_remote_record(
                            execution_dir=paths.execution_dir,
                            task_id=task_id,
                            record=remote_record,
                        )

            def _run_repair_agent(repair_instruction: str, attempt: int) -> int:
                """Re-run the executing agent on this task with a repair prompt.

                Same machinery as the original invocation -- same config, mode,
                write guard, budget and MCP scope -- only the instruction and the
                session id differ, so a repair is not a weaker kind of run.
                """
                repair_session_id = f"{runtime_session_id}-repair{attempt}"
                repair_sessions_dir = _execution_private_sessions_dir(
                    cfg=run_cfg,
                    run_id=paths.run_id,
                    task_id=f"{task_id}-repair{attempt}",
                    workspace_root=paths.root,
                )
                _cleanup_execution_private_sessions_dir(repair_sessions_dir)
                repair_mcp_manager: Any | None = None
                try:
                    repair_mcp_manager = _build_forge_task_scoped_mcp_manager(
                        workspace_root=paths.root,
                        session_id=repair_session_id,
                        task_scope=task_mcp_scope,
                    )
                    if asset_surface is not None and has_mirrored_task_assets:
                        repair_mcp_manager = compose_worker_asset_mcp_manager(
                            base_manager=repair_mcp_manager,
                            asset_manager=build_worker_asset_mcp_manager(
                                cfg=run_cfg,
                                surface=asset_surface,
                                model_registry=asset_model_registry,
                                mirror=task_asset_mirror,
                                usage_logger=asset_usage_logger,
                                api_key=api_key_override,
                            ),
                        )
                    return run_agent(
                        cfg=run_cfg,
                        root=paths.root,
                        instruction=repair_instruction,
                        mode=effective_mode,
                        runtime_kind=RuntimeKind.FORGE_EXEC,
                        yes=yes,
                        max_steps=task_step_budget.resolved_max_steps,
                        no_log=no_log,
                        api_key_override=api_key_override,
                        console=console,
                        surface=recording_surface,
                        image_paths=task_image_paths,
                        deny_write_prefixes=[".alysis"],
                        allow_write_globs=allowed_scope if scope_mode == "strict" else None,
                        non_interactive=run_non_interactive,
                        session_log_dir_override=repair_sessions_dir,
                        session_id_override=repair_session_id,
                        usage_role=f"forge_exec:{task_id}:repair{attempt}",
                        enable_compaction=False,
                        enable_tool_output_offload=True,
                        enable_conversation_summarization=True,
                        compaction_profile="execution",
                        enable_chat_turn_step_budget=False,
                        one_shot_execution=True,
                        verification_enabled=prompt_verification_enabled,
                        authoritative_verification_commands=(
                            verify_commands if prompt_verification_enabled else None
                        ),
                        subagents_enabled=False,
                        enforce_explicit_subagent_requests=False,
                        mcp_manager=repair_mcp_manager,
                        session_source_metadata={
                            "surface": "forge_exec",
                            "run_id": paths.run_id,
                            "task_id": str(task_id),
                            "verification_repair_attempt": int(attempt),
                        },
                    )
                finally:
                    if repair_mcp_manager is not None:
                        repair_mcp_manager.close()
                    _cleanup_execution_private_sessions_dir(repair_sessions_dir)

            if success and verify_mode != "off" and verify_commands:
                verify_mutation_paths: list[str] = []
                verify_mutation_patch_sections: list[str] = []

                def _verification_pass(pass_label: str) -> Any:
                    """Run the gate once, recording anything it changed on disk.

                    Each pass is snapshotted separately so a repair attempt's own
                    (committed) edits between passes are never mistaken for a
                    verification command mutating the repository.
                    """
                    before_verify_snapshot = _snapshot_workspace_tree(paths.root)
                    result = run_task_verification(
                        root=paths.root,
                        commands=verify_commands,
                        artifact_path=verify_path,
                        cfg=effective,
                    )
                    events.emit(
                        EVENT_VERIFICATION_RESULT,
                        {
                            "scope": "task",
                            "task_id": task_id,
                            "passed": bool(result.all_passed),
                            "policy": verify_mode,
                            "pass": pass_label,
                            "summary": result.summary,
                            "failure_category": result.failure_category_value,
                            "commands": list(verify_commands),
                            "command_source": verify_command_source,
                            "artifact": os.fspath(verify_path),
                            "result": verify_run_result_to_payload(
                                root=paths.root,
                                result=result,
                            ),
                        },
                    )
                    after_verify_snapshot = _snapshot_workspace_tree(paths.root)
                    pass_diff = _build_workspace_snapshot_reporting_diff(
                        paths.root,
                        before_snapshot=before_verify_snapshot,
                        after_snapshot=after_verify_snapshot,
                    )
                    for path in pass_diff.changed_files:
                        if path not in verify_mutation_paths:
                            verify_mutation_paths.append(path)
                    if pass_diff.patch_text:
                        verify_mutation_patch_sections.append(pass_diff.patch_text)
                    return result

                verify_result = _verification_pass("initial")

                # A failing gate is a signal about the work, not a verdict on it:
                # hand the failing output back to the same agent and let it fix
                # what it broke before the task is called failed. Only in strict
                # mode, because that is the only mode where the failure blocks.
                repair_outcome = None
                if (
                    not verify_result.all_passed
                    and verify_mode == "strict"
                    and verify_repair_budget > 0
                ):
                    repair_outcome = run_verification_repair_loop(
                        initial_result=verify_result,
                        max_attempts=verify_repair_budget,
                        repairable=lambda result: (
                            result.failure_category_value != FailureCategory.INFRA_UNAVAILABLE.value
                        ),
                        attempt_repair=lambda attempt, failing: _attempt_verification_repair(
                            attempt=attempt,
                            failing_result=failing,
                            max_attempts=verify_repair_budget,
                            task_id=task_id,
                            base_instruction=instruction,
                            verify_artifact_path=verify_path,
                            run_verification=lambda: _verification_pass(f"repair.{attempt}"),
                            run_repair_agent=_run_repair_agent,
                            root=paths.root,
                            commit_message=(f"{task_id}: verification repair attempt {attempt}"),
                        ),
                    )
                    verify_result = repair_outcome.final_result
                    verification_repair_payload = repair_outcome.to_payload()
                    for line in repair_outcome.report_lines():
                        scope_warnings.append(line)
                    if repair_outcome.attempts:
                        try:
                            patch_text = format_patch_stdout(
                                paths.root,
                                base_branch=pr_base_branch,
                            )
                            patch_path.write_text(
                                patch_text if patch_text else "(empty format-patch output)\n",
                                encoding="utf-8",
                            )
                            changed_files = changed_files_between(
                                paths.root,
                                revspec=f"{pr_base_branch}..HEAD",
                            )
                        except GitOpsError as e:
                            scope_warnings.append(
                                f"Could not refresh the patch after verification repair: {e}"
                            )

                verify_summary = verify_result.summary
                verify_payload = verify_run_result_to_payload(
                    root=paths.root,
                    result=verify_result,
                )
                if repair_outcome is not None and repair_outcome.attempts:
                    verify_summary = (
                        f"{verify_summary} "
                        f"(after {repair_outcome.attempts_used} repair attempt"
                        f"{'s' if repair_outcome.attempts_used != 1 else ''})"
                    )
                verify_mutation_diff_text = "\n".join(verify_mutation_patch_sections)
                if verify_mutation_paths:
                    preview = ", ".join(verify_mutation_paths[:20])
                    if len(verify_mutation_paths) > 20:
                        preview += ", ..."
                    verify_mutation_msg = (
                        "Verification commands modified repository state after the task commit "
                        f"({len(verify_mutation_paths)}): {preview}."
                    )
                    if scope_mode in {"warn", "strict"}:
                        scope_assessment = assess_scope_changes(
                            verify_mutation_paths,
                            allowed_scope,
                            task=task,
                            root=paths.root,
                        )
                        scope_diagnostics.extend(
                            item.to_payload() for item in scope_assessment.diagnostics
                        )
                        if not scope_assessment.ok:
                            verify_scope_violations = scope_assessment.blocking_paths
                            scope_violation_files = _merge_changed_files(
                                scope_violation_files,
                                verify_scope_violations,
                            )
                            classes = sorted(
                                {
                                    item.classification
                                    for item in scope_assessment.diagnostics
                                    if not item.allowed
                                }
                            )
                            scope_msg = (
                                f"Out-of-scope file changes detected ({len(verify_scope_violations)}): "
                                f"{', '.join(verify_scope_violations[:20])}"
                            )
                            if len(verify_scope_violations) > 20:
                                scope_msg += ", ..."
                            scope_msg += (
                                f". Allowed scope: {allowed_scope or ['(none)']}."
                                " Task was blocked due to strict scope isolation."
                                " Verification commands modified repository state after the task commit."
                            )
                            if classes:
                                scope_msg += f" Scope classifications: {', '.join(classes)}."
                            if scope_mode == "strict":
                                success = False
                                commit_hash = None
                                merge_result = "not merged: strict scope isolation blocked verification-time writes"
                                run_err = (run_err + "; " if run_err else "") + scope_msg
                                changed_files = _merge_changed_files(
                                    changed_files,
                                    verify_mutation_paths,
                                )
                                _append_patch_debug_section(
                                    patch_path,
                                    title="Post-verification workspace diff",
                                    patch_text=verify_mutation_diff_text,
                                )
                            else:
                                scope_warnings.append(scope_msg)
                        elif scope_mode == "strict":
                            success = False
                            commit_hash = None
                            merge_result = (
                                "not merged: verification commands modified repository state"
                            )
                            run_err = (run_err + "; " if run_err else "") + verify_mutation_msg
                            changed_files = _merge_changed_files(
                                changed_files, verify_mutation_paths
                            )
                            _append_patch_debug_section(
                                patch_path,
                                title="Post-verification workspace diff",
                                patch_text=verify_mutation_diff_text,
                            )
                        else:
                            scope_warnings.append(verify_mutation_msg)
                if not verify_result.all_passed and verify_mode == "strict":
                    success = False
                    verify_blocked = True
                    merge_result = "not merged: strict verification failed"
                    run_err = (run_err + "; " if run_err else "") + (
                        f"verification failed: {verify_result.summary}"
                    )
                elif not verify_result.all_passed:
                    warning_prefix = (
                        "Verification infrastructure warning"
                        if verify_result.failure_category_value
                        == FailureCategory.INFRA_UNAVAILABLE.value
                        else "Verification warning"
                    )
                    scope_warnings.append(f"{warning_prefix}: {verify_result.summary}")
            elif success and verify_mode == "strict":
                # The work is committed on the task branch and no authoritative
                # command exists to check it -- a property of the workspace, not
                # a defect in the work. Failing here used to discard a completed
                # task over missing tooling, so the task now completes unverified:
                # nothing is merged, the branch stays intact for review, and the
                # honest outcome is recorded instead of a fabricated failure.
                verification_unavailable_completion = True
                verify_summary = (
                    "verification skipped: no authoritative commands available; "
                    "task kept as completed_unverified"
                )
                merge_result = (
                    "not merged: verification unavailable "
                    f"(branch {pr_task_branch} kept for review)"
                )
                scope_warnings.append(
                    "Strict verification found no authoritative command for this task. "
                    "The work was kept and committed, but nothing checked it -- review "
                    "the branch before merging, or provide --verify-cmd."
                )
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                    outcome=TASK_STATUS_COMPLETED_UNVERIFIED,
                )
            elif verify_mode == "strict":
                # Reached only when the task already failed for another reason;
                # strict verification simply had nothing to add.
                success = False
                verify_blocked = True
                merge_result = "not merged: strict verification unavailable"
                run_err = (run_err + "; " if run_err else "") + (
                    "strict verification requires authoritative commands, but none were available"
                )
                verify_summary = "verification skipped: no authoritative commands available"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=True,
                )
            elif verify_mode != "off":
                verify_summary = "verification skipped: no authoritative commands available"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                )
            elif verify_mode == "off":
                verify_summary = "verification disabled (--verify off)"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                )

            if success and review:
                try:
                    review_outcome = review_task(
                        paths=paths,
                        plan=plan,
                        task=task,
                        cfg=effective,
                        api_key_override=api_key_override,
                        verification_payload_override=verify_payload,
                    )
                    events.emit(
                        EVENT_REVIEW_RESULT,
                        {
                            "task_id": task_id,
                            "approved": bool(review_outcome.approved),
                            "confidence": review_outcome.confidence,
                            "summary": review_outcome.summary,
                            "blocking_issues": review_outcome.blocking_issues_count,
                            "non_blocking_issues": review_outcome.non_blocking_issues_count,
                            "review_json": os.fspath(review_outcome.json_path),
                            "review_markdown": os.fspath(review_outcome.markdown_path),
                        },
                    )
                    if not review_outcome.approved:
                        success = False
                        review_blocked = True
                        merge_result = "not merged: review requested changes"
                except ReviewError as e:
                    success = False
                    run_err = (run_err + "; " if run_err else "") + f"review failed: {e}"
                    merge_result = f"not merged: review failed: {e}"
                    events.emit(
                        EVENT_REVIEW_RESULT,
                        {
                            "task_id": task_id,
                            "approved": False,
                            "available": False,
                            "error": str(e),
                        },
                    )

            if success and verification_unavailable_completion:
                # Nothing merges without a check behind it. Return the worktree to
                # the base branch so the run leaves the repository where it found
                # it, and leave the task branch in place for a human to review.
                try:
                    checkout_branch(
                        paths.root,
                        pr_base_branch,
                        base_branch=pr_base_branch,
                    )
                except GitOpsError as e:
                    scope_warnings.append(
                        "Could not return to "
                        f"{pr_base_branch} after an unverified completion: {e}. "
                        f"The work is committed on {pr_task_branch}."
                    )
            elif success:
                try:
                    merge_title = str(task.get("title") or "").strip()
                    merge_message = (
                        f"Merge {task_id}: {merge_title}" if merge_title else f"Merge {task_id}"
                    )
                    merge_commit_hash = merge_no_ff(
                        paths.root,
                        base_branch=pr_base_branch,
                        task_branch=pr_task_branch,
                        message=merge_message,
                    )
                    if keep_branch:
                        merge_result = f"merged into {pr_base_branch} (branch kept)"
                    else:
                        try:
                            delete_branch(paths.root, pr_task_branch)
                            merge_result = f"merged into {pr_base_branch} (branch deleted)"
                        except GitOpsError as cleanup_err:
                            scope_warnings.append(
                                "Branch cleanup warning: "
                                f"failed to delete {pr_task_branch}: {cleanup_err}"
                            )
                            merge_result = (
                                f"merged into {pr_base_branch} "
                                f"(branch delete warning: {cleanup_err})"
                            )
                except GitOpsError as e:
                    success = False
                    run_err = (run_err + "; " if run_err else "") + f"PR flow failed: {e}"
                    unmerged = list_unmerged_files(paths.root)
                    if unmerged and pr_base_branch and pr_task_branch:
                        merge_conflict_detected = True
                        context = capture_merge_conflict_context(
                            paths.root,
                            base_branch=pr_base_branch,
                            task_branch=pr_task_branch,
                            merge_error=str(e),
                        )
                        review_outcome = review_merge_conflict(
                            paths=paths,
                            task=task,
                            cfg=effective,
                            api_key_override=api_key_override,
                            context=context,
                            plan=plan,
                        )
                        cleanup_ok, cleanup_log = try_abort_merge(
                            paths.root,
                            base_branch=pr_base_branch,
                        )
                        conflict_artifacts = write_conflict_artifacts(
                            paths=paths,
                            task_id=task_id,
                            context=context,
                            review_json=review_outcome.review_json,
                            review_md=review_outcome.review_markdown,
                            cleanup_log=cleanup_log,
                        )
                        conflict_review_path = conflict_artifacts.review_md_path
                        merge_result = (
                            f"not merged: conflict while merging {pr_task_branch} into "
                            f"{pr_base_branch}"
                        )
                        if review_outcome.skipped_reason:
                            scope_warnings.append(
                                f"Conflict review note: {review_outcome.skipped_reason}"
                            )
                        if not cleanup_ok:
                            scope_warnings.append(
                                "Merge cleanup warning: repository state may need manual recovery. "
                                f"See {conflict_artifacts.cleanup_log_path}"
                            )
                        if auto_resolve_conflicts and can_attempt_conflict_auto_resolve(
                            task=task,
                            settings=conflict_auto_settings,
                        ):
                            bump_conflict_attempt(task)
                            save_plan(paths, plan)
                            auto_outcome = attempt_auto_resolve_conflict(
                                paths=paths,
                                plan=plan,
                                task=task,
                                cfg=effective,
                                api_key_override=api_key_override,
                                base_branch=pr_base_branch,
                                task_branch=pr_task_branch,
                                keep_worktrees=False,
                                settings=conflict_auto_settings,
                                verify_commands=(verify_commands if verify_mode != "off" else []),
                            )
                            if auto_outcome.success:
                                success = True
                                run_err = None
                                merge_conflict_detected = False
                                merge_commit_hash = auto_outcome.merge_commit_hash
                                merge_result = f"auto-resolved and merged into {pr_base_branch}"
                                if auto_outcome.warnings:
                                    scope_warnings.extend(auto_outcome.warnings)
                                conflict_review_path = auto_outcome.report_path
                            else:
                                scope_warnings.append(
                                    "Conflict auto-resolve failed: "
                                    f"{auto_outcome.error or 'unknown error'}"
                                )
                        else:
                            # The sequential path stops at a conflict instead of
                            # starting a resolver agent in its own worktree. That
                            # is swarm machinery, and reaching for it here turned
                            # "one task did not merge" into a second opaque agent
                            # run. Report the conflict and how to finish it.
                            scope_warnings.extend(
                                _sequential_conflict_report_lines(
                                    root=paths.root,
                                    task_id=task_id,
                                    base_branch=pr_base_branch,
                                    task_branch=pr_task_branch,
                                    review_path=conflict_review_path,
                                )
                            )
                    else:
                        merge_result = f"not merged: {e}"

            if success and remote_settings.enabled and remote_record is not None and pr_base_branch:
                pushed_base, base_output = push_base(
                    paths.root,
                    remote=str(remote_record.get("remote") or remote_settings.remote_name),
                    base_branch=pr_base_branch,
                )
                remote_record["pushed_base"] = pushed_base
                remote_record["base_push_output"] = truncate_output(base_output)
                if not pushed_base:
                    msg = f"remote base push failed: {base_output or 'unknown error'}"
                    raw_errors = remote_record.get("errors")
                    if isinstance(raw_errors, list):
                        raw_errors.append(msg)
                    # Local merge already happened; keep success and record warning.
                    scope_warnings.append(msg)
                write_remote_record(
                    execution_dir=paths.execution_dir,
                    task_id=task_id,
                    record=remote_record,
                )

    if pr and merge_result is None:
        merge_result = "not merged"

    if pr and not pr_report_state_upgraded:
        recovered_pr_report_state = False
        if commit_hash is not None and pr_base_branch:
            try:
                patch_text = format_patch_stdout(paths.root, base_branch=pr_base_branch)
                patch_path.write_text(
                    patch_text if patch_text else "(empty format-patch output)\n",
                    encoding="utf-8",
                )
                changed_files = changed_files_between(
                    paths.root,
                    revspec=f"{pr_base_branch}..HEAD",
                )
                recovered_pr_report_state = True
            except GitOpsError:
                recovered_pr_report_state = False
        if not recovered_pr_report_state:
            patch_path.write_text(report_diff.patch_text, encoding="utf-8")
            changed_files = list(report_diff.changed_files)

    if runtime_artifacts_changed:
        summary = "Task failed: agent modified files under .alysis/ which is not allowed."
    elif scope_violation_files:
        summary = "Task blocked due to strict scope isolation."
    elif no_material_changes_blocked:
        summary = "Task failed: no material file changes were detected."
    elif verify_blocked:
        summary = "Task blocked by strict verification gate."
    elif review_blocked:
        summary = "Task blocked by review gate (changes requested)."
    elif analysis_only_noop_accepted:
        summary = "Analysis-only task completed successfully with no repository changes."
    elif success and verification_unavailable_completion:
        summary = (
            "Task completed, but nothing verified it: no authoritative verification "
            f"command exists for this workspace. The work is committed on "
            f"{pr_task_branch or 'the task branch'} and was deliberately not merged."
        )
    elif run_code == 0 and success:
        summary = "Task execution completed successfully."
    elif pr and can_attempt_pr_flow and run_code == 0:
        summary = "Task execution failed during PR flow."
    else:
        summary = "Task execution failed."
    if run_err:
        summary += f" Error: {run_err}"
    if scope_warnings:
        summary += " Warnings: " + " | ".join(scope_warnings)
    if conflict_review_path is not None:
        summary += f" Conflict review: {conflict_review_path}"

    finished_at = now_iso()
    report_verify_commands = verify_commands if pr and verify_mode != "off" else []
    report_path = write_task_report(
        paths=paths,
        task=task,
        result="success" if success else "failure",
        result_kind=result_kind,
        summary=summary,
        started_at=started_at,
        finished_at=finished_at,
        changed_files=changed_files,
        verify_commands=report_verify_commands,
        patch_path=patch_path,
        budget_artifact_path=budget_artifact_path,
        execution_log_artifacts=exec_artifacts,
        verify_artifact_path=verify_path if verify_path.exists() else None,
        verify_summary=verify_summary,
        verify_payload=verify_payload,
        verify_command_source=verify_command_source,
        base_branch=pr_base_branch,
        task_branch=pr_task_branch,
        commit_hash=commit_hash,
        merge_commit_hash=merge_commit_hash,
        merge_result=merge_result,
        salvaged_nonzero_exit=bool(pr_nonzero_salvage_attempted and success),
        noop_reason=noop_reason,
        remote_lines=_remote_report_lines(remote_record),
        scope_amendments=scope_amendment_payloads,
        scope_amended_patterns=scope_amended_patterns,
    )
    persisted_capture = persist_execution_knowledge_capture(
        paths=paths,
        task=task,
        source="forge_exec",
        assistant_message=recording_surface.final_assistant_message,
        artifact_dir=(
            paths.execution_knowledge_capture_dir
            / _safe_task_file_component(task_id)
            / _safe_task_file_component(started_at)
        ),
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_path if verify_path.exists() else None,
        budget_artifact_path=budget_artifact_path,
        session_artifact_dir=exec_artifacts.session_artifact_dir,
    )
    if success and verification_unavailable_completion:
        # The work is kept, but "validated" would be a lie: nothing ran against it.
        mark_knowledge_capture_promotion_skipped(
            artifact_dir=persisted_capture.artifact_dir,
            reason="task completed without any authoritative verification",
        )
    elif success:
        promote_validated_knowledge_capture(
            paths=paths,
            task=task,
            artifact_dir=persisted_capture.artifact_dir,
        )
    else:
        mark_knowledge_capture_promotion_skipped(
            artifact_dir=persisted_capture.artifact_dir,
            reason="task execution outcome was not accepted",
        )

    write_task_attempt_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        result="success" if success else "failure",
        summary=summary,
        changed_files=changed_files,
        verify_summary=verify_summary,
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_path if verify_path.exists() else None,
        budget_artifact_path=budget_artifact_path,
        session_artifact_dir=exec_artifacts.session_artifact_dir,
        acceptance_state="accepted" if success else "rejected",
        extra_tags=[
            "execution",
            "sequential",
        ],
    )
    issue_paths = changed_files or list(allowed_scope)
    if runtime_artifacts_changed:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: protected .alysis mutation attempt",
            summary="Engineer execution attempted to modify protected .alysis runtime state.",
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["protected_runtime_mutation"],
        )
    elif verify_blocked:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: verification failed",
            summary=verify_summary or "Verification blocked task completion.",
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["verification_failure"],
        )
    elif review_blocked:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: review requested changes",
            summary=summary,
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["review_blocked"],
        )
    elif merge_conflict_detected:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: merge conflict remains unresolved",
            summary=summary,
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["merge_conflict"],
        )
    elif not success:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: task execution failed",
            summary=summary,
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=(
                ["execution_failure", "scope_violation"]
                if scope_violation_files
                else ["execution_failure"]
            ),
        )
    rebuild_knowledge_index(paths)

    if success and verification_unavailable_completion:
        status = TASK_STATUS_COMPLETED_UNVERIFIED
    elif success:
        status = "done"
    elif merge_conflict_detected:
        status = "merge_conflict"
    elif verify_blocked:
        status = "verify_failed"
    elif review_blocked:
        status = "changes_requested"
    else:
        status = "failed"
    set_task_status(plan, task_id, status)
    save_plan(paths, plan)

    console.print(f"Task: {task_id} ({task.get('title', '')})")
    if success and verification_unavailable_completion:
        console.print("Result: completed_unverified (kept, but nothing verified it)")
    else:
        console.print(f"Result: {'success' if success else 'failure'}")
    console.print(f"Report: {report_path}")
    console.print(f"Patch: {patch_path}")
    _print_usage_summary_from_logs(
        console=console,
        title=f"Usage Summary ({task_id})",
        log_paths=([exec_artifacts.log_copy_path] if exec_artifacts.log_retained else []),
    )
    if conflict_review_path is not None:
        console.print(f"Conflict Review: {conflict_review_path}")

    task_payload: dict[str, Any] = {
        "task_id": task_id,
        "title": str(task.get("title", "")),
        "status": status,
        "source": "exec",
        "success": bool(success),
        "report": os.fspath(report_path),
        "patch": os.fspath(patch_path),
        "verify_summary": verify_summary,
        "verify_blocked": bool(verify_blocked),
        "verification_unavailable": bool(verification_unavailable_completion),
        "verification_repair": (
            verification_repair_payload if verification_repair_payload else None
        ),
        "review_blocked": bool(review_blocked),
        "merge_conflict": bool(merge_conflict_detected),
        "merge_result": merge_result,
        "branch": pr_task_branch,
        "base_branch": pr_base_branch,
        "commit": commit_hash,
        "merge_commit": merge_commit_hash,
        "scope_warnings": list(scope_warnings),
        "scope_amendments": list(scope_amendment_payloads),
        "scope_amended_patterns": list(scope_amended_patterns),
    }
    if conflict_review_path is not None:
        task_payload["conflict_review"] = os.fspath(conflict_review_path)
    events.emit(
        EVENT_TASK_COMPLETED if success else EVENT_TASK_FAILED,
        task_payload,
    )
    # A task that ran and was not accepted is a genuine execution failure, so exit 1
    # stays. Exit 2 is reserved for the command itself failing, which leaves this
    # function as ForgeTaskExecutionError rather than as an outcome.
    return TaskExecutionOutcome(
        task_id=task_id,
        title=str(task.get("title", "")),
        status=status,
        success=bool(success),
        exit_code=EXIT_OK if success else EXIT_NOT_ACCEPTED,
        summary=summary,
        report_path=report_path,
        patch_path=patch_path,
        merge_conflict=bool(merge_conflict_detected),
        conflict_review_path=conflict_review_path,
        payload=task_payload,
    )


def forge_exec(
    task_id: str = typer.Argument(..., help="Task id from plan.json (for example T01)."),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    pr: bool = typer.Option(
        False,
        "--pr/--no-pr",
        help="Run task in PR-like git flow (branch, commit, patch, merge).",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run automated PR review gate before merge (requires --pr).",
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch for --pr mode (defaults to current branch).",
    ),
    keep_branch: bool = typer.Option(
        False,
        "--keep-branch",
        help="Keep task branch after successful merge in --pr mode.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Verification policy for PR flow: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    # Appended after ``yes`` on purpose: `forge_exec_impl` forwards these positionally,
    # so a new parameter inserted mid-list would silently shift every later argument.
    verify_repair_attempts: int | None = typer.Option(
        None,
        "--verify-repair-attempts",
        help=(
            "How many times a failing strict verification is fed back to the agent "
            "to repair before the task fails (default 2; 0 disables)."
        ),
    ),
    auto_resolve_conflicts: bool = typer.Option(
        False,
        "--auto-resolve-conflicts",
        help=(
            "On merge conflict, start a resolver agent in a dedicated worktree instead "
            "of stopping with a conflict report. Off by default: the sequential path "
            "reports the conflict and tells you how to land it."
        ),
    ),
    events: Any = None,
) -> None:
    events = _events_or_null(events)
    console = _console()
    cfg = load_config()
    effective = clone_cfg(cfg)
    current_ctx = get_current_context(silent=True)
    max_steps_source = (
        current_ctx.get_parameter_source("max_steps") if current_ctx is not None else None
    )
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        max_steps_provided = (
            max_steps_source is not None and max_steps_source is not ParameterSource.DEFAULT
        )
    if base_url is not None:
        effective.base_url = base_url
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    if stream is not None:
        effective.stream = stream
    if max_steps is not None:
        effective.max_steps = max_steps
    effective_mode = (mode.value if mode else effective.default_mode) or "review"
    scope_mode = "strict"
    verify_mode = "warn"
    verify_commands: list[str] = []
    verify_command_source: str | None = None
    run_cfg = clone_cfg(effective)
    # Called as a plain function (not through Typer) this arrives as an OptionInfo,
    # which is neither an int nor None -- normalize before it reaches the budget.
    repair_attempts_override = (
        verify_repair_attempts
        if isinstance(verify_repair_attempts, int) and not isinstance(verify_repair_attempts, bool)
        else None
    )
    verify_repair_budget = resolve_repair_attempt_budget(repair_attempts_override)

    try:
        scope_mode = _normalize_scope_mode(scope)
        verify_mode = _normalize_verify_mode(verify)
        api_key_override = _resolve_api_key_override(
            api_key=api_key,
            api_key_env=api_key_env,
            api_key_stdin=api_key_stdin,
        )
        paths = load_current_run_paths(path)
        events.set_run_id(paths.run_id)
        plan = load_plan(paths)
        run_cfg.model = resolve_model_for_role(
            cfg=effective,
            role=ROLE_CODING,
            plan=plan,
            prefer_context="forge",
        )
    except (ConfigError, ForgeError) as e:
        raise _exec_error_exit(console, events, str(e), task_id=task_id) from e

    task = find_task(plan, task_id)
    if task is None:
        message = f"Task not found: {task_id}"
        events.emit(
            EVENT_PLAN_INVALID,
            {"reason": message, "source": "task_lookup", "task_id": task_id},
        )
        raise _exec_error_exit(console, events, message, task_id=task_id)
    task_status = canonical_task_status(str(task.get("status") or ""))
    if task_status in {"superseded", "invalidated"}:
        message = (
            f"Task is non-executable obsolete work ({task_status}): {task_id}. "
            "Use an active planned replacement task instead."
        )
        events.emit(
            EVENT_PLAN_INVALID,
            {
                "reason": message,
                "source": "task_status",
                "task_id": task_id,
                "status": task_status,
            },
        )
        raise _exec_error_exit(console, events, message, task_id=task_id)
    if verify_mode != "off":
        verify_selection = resolve_authoritative_task_verify_command_selection(
            cfg=effective,
            verify_cmd=verify_cmd,
            task=task,
            root=paths.root,
            plan_requirements=[
                str(item).strip() for item in (plan.get("requirements") or []) if str(item).strip()
            ],
        )
        verify_commands = list(verify_selection.commands)
        verify_command_source = verify_selection.source
        run_cfg.verify_commands = list(verify_commands)

    blockers = _task_dependency_blockers(plan, task)
    if blockers:
        message = "Dependencies are not done: " + ", ".join(blockers)
        events.emit(
            EVENT_PLAN_INVALID,
            {
                "reason": message,
                "source": "dependencies",
                "task_id": task_id,
                "blockers": list(blockers),
            },
        )
        raise _exec_error_exit(console, events, message, task_id=task_id)
    if review and not pr:
        raise _exec_error_exit(
            console,
            events,
            "--review requires --pr.",
            task_id=task_id,
            kind="usage_error",
        )

    try:
        run_mutation_guard = acquire_swarm_mutation_guard(
            paths,
            mode=f"forge_exec:{task_id}",
            on_wait=lambda info: _print_forge_lock_wait_notice(console, info),
        )
    except ForgeError as e:
        raise _exec_error_exit(console, events, str(e), task_id=task_id) from e

    try:
        if bool(getattr(run_mutation_guard, "acquired_after_wait", False)):
            plan = load_plan(paths)
            task = find_task(plan, task_id)
            if task is None:
                message = (
                    "Queued Forge exec revalidated the current plan "
                    f"and task no longer exists: {task_id}"
                )
                events.emit(
                    EVENT_PLAN_INVALID,
                    {"reason": message, "source": "task_lookup", "task_id": task_id},
                )
                raise _exec_error_exit(console, events, message, task_id=task_id)
            task_status = canonical_task_status(str(task.get("status") or ""))
            if task_status in {"done", "superseded", "invalidated"}:
                message = (
                    "Queued Forge exec revalidated the current plan "
                    f"and task is no longer executable ({task_status}): {task_id}."
                )
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": message,
                        "source": "task_status",
                        "task_id": task_id,
                        "status": task_status,
                    },
                )
                raise _exec_error_exit(console, events, message, task_id=task_id)
            run_cfg.model = resolve_model_for_role(
                cfg=effective,
                role=ROLE_CODING,
                plan=plan,
                prefer_context="forge",
            )
            verify_commands = []
            verify_command_source = None
            if verify_mode != "off":
                verify_selection = resolve_authoritative_task_verify_command_selection(
                    cfg=effective,
                    verify_cmd=verify_cmd,
                    task=task,
                    root=paths.root,
                    plan_requirements=[
                        str(item).strip()
                        for item in (plan.get("requirements") or [])
                        if str(item).strip()
                    ],
                )
                verify_commands = list(verify_selection.commands)
                verify_command_source = verify_selection.source
                run_cfg.verify_commands = list(verify_commands)
            blockers = _task_dependency_blockers(plan, task)
            if blockers:
                message = "Dependencies are not done: " + ", ".join(blockers)
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": message,
                        "source": "dependencies",
                        "task_id": task_id,
                        "blockers": list(blockers),
                    },
                )
                raise _exec_error_exit(console, events, message, task_id=task_id)

        _mark_run_status(
            paths,
            RUN_STATUS_RUNNING,
            reason=f"forge exec started for {task_id}",
            plan=plan,
            mode=f"forge_exec:{task_id}",
        )
        outcome = execute_forge_task(
            console=console,
            events=events,
            paths=paths,
            plan=plan,
            task=task,
            task_id=task_id,
            effective=effective,
            run_cfg=run_cfg,
            effective_mode=effective_mode,
            scope_mode=scope_mode,
            verify_mode=verify_mode,
            verify_commands=verify_commands,
            verify_command_source=verify_command_source,
            verify_repair_budget=verify_repair_budget,
            api_key_override=api_key_override,
            yes=yes,
            no_log=no_log,
            max_steps_provided=max_steps_provided,
            pr=pr,
            review=review,
            base_branch=base_branch,
            keep_branch=keep_branch,
            auto_resolve_conflicts=auto_resolve_conflicts is True,
        )
        terminal_status, terminal_reason = _terminal_run_status_for(
            plan=plan,
            any_failure=not outcome.success,
        )
        _mark_run_status(paths, terminal_status, reason=terminal_reason)
    except ForgeTaskExecutionError as e:
        raise _exec_error_exit(
            console,
            events,
            str(e),
            task_id=e.task_id or task_id,
            kind=e.kind,
        ) from e
    finally:
        _mark_run_interrupted_if_still_running(
            paths,
            reason=f"forge exec for {task_id} exited without recording an outcome",
        )
        run_mutation_guard.release()

    events.run_completed(
        ok=outcome.success,
        exit_code=outcome.exit_code,
        data={"task": outcome.payload},
    )
    raise typer.Exit(code=outcome.exit_code)


def forge_run(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of requiring a narrower project path.",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated task ids to execute (dependencies are still enforced).",
    ),
    max_tasks: int | None = typer.Option(
        None,
        "--max-tasks",
        min=1,
        help="Stop after this many tasks have been executed.",
    ),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        min=1,
        help="Skip tasks that already reached this many recorded attempts.",
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Include tasks currently marked failed.",
    ),
    retry_changes_requested: bool = typer.Option(
        False,
        "--retry-changes-requested",
        help="Include tasks currently marked changes_requested.",
    ),
    keep_going: bool = typer.Option(
        False,
        "--keep-going",
        help=(
            "Continue with the next independent task after a failure instead of stopping. "
            "Default is to stop, because a failed task usually leaves later tasks unsound."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the execution order and exit without running anything.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Per-task verification policy: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    verify_repair_attempts: int | None = typer.Option(
        None,
        "--verify-repair-attempts",
        help=(
            "How many times a failing strict verification is fed back to the agent "
            "to repair before the task fails (default 2; 0 disables)."
        ),
    ),
    pr: bool = typer.Option(
        True,
        "--pr/--no-pr",
        help=(
            "Run each task as a branch/commit/verify/merge cycle in the main checkout "
            "(default). --no-pr leaves the changes uncommitted and runs no verification "
            "gate, so use it only where git flow is unavailable."
        ),
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run the automated review gate before merging each task (requires --pr).",
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch for --pr mode (defaults to the current branch).",
    ),
    keep_branch: bool = typer.Option(
        False,
        "--keep-branch",
        help="Keep each task branch after a successful merge.",
    ),
    auto_resolve_conflicts: bool = typer.Option(
        False,
        "--auto-resolve-conflicts",
        help=(
            "On merge conflict, start a resolver agent in a dedicated worktree instead of "
            "stopping with a conflict report. Off by default."
        ),
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    cli_ctx: Any = None,
    events: Any = None,
) -> None:
    """Execute every ready task sequentially, in dependency order, in this checkout.

    No worktrees, no parallelism, no batch integration gate: one task at a time
    through :func:`execute_forge_task`, the same core `forge exec` uses. The run
    stops at the first failure unless ``--keep-going`` says otherwise.
    """
    events = _events_or_null(events)
    console = _console()
    cfg = load_config()
    effective = clone_cfg(cfg)
    # The public Typer command forwards its own context explicitly. Prefer it
    # over Click's ambient lookup: this implementation is called through a
    # delegation layer, where the ambient context may be the parent ``forge``
    # group and cannot report whether run-specific options were supplied.
    current_ctx = cli_ctx if cli_ctx is not None else get_current_context(silent=True)
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        source = current_ctx.get_parameter_source("max_steps")
        # Typer may provide its vendored Click enum here, so compare the stable
        # semantic name rather than enum identity across package boundaries.
        max_steps_provided = (
            source is not None and getattr(source, "name", None) != ParameterSource.DEFAULT.name
        )
    pr_explicit = False
    if current_ctx is not None:
        pr_source = current_ctx.get_parameter_source("pr")
        pr_explicit = (
            pr_source is not None
            and getattr(pr_source, "name", None) != ParameterSource.DEFAULT.name
        )

    if base_url is not None:
        effective.base_url = base_url
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    if stream is not None:
        effective.stream = stream
    if max_steps is not None:
        effective.max_steps = max_steps
    effective_mode = (mode.value if mode else effective.default_mode) or "review"

    # Called as a plain function these arrive as truthy typer OptionInfo objects,
    # so every boolean is normalized before it can silently flip a policy.
    pr_requested = pr is True
    review_requested = review is True
    keep_branch_requested = keep_branch is True
    keep_going_requested = keep_going is True
    dry_run_requested = dry_run is True
    retry_failed_requested = retry_failed is True
    retry_changes_requested_requested = retry_changes_requested is True
    auto_resolve_requested = auto_resolve_conflicts is True
    repair_attempts_override = (
        verify_repair_attempts
        if isinstance(verify_repair_attempts, int) and not isinstance(verify_repair_attempts, bool)
        else None
    )
    verify_repair_budget = resolve_repair_attempt_budget(repair_attempts_override)
    only_ids = _parse_only_task_ids(only)
    task_limit = (
        max_tasks if isinstance(max_tasks, int) and not isinstance(max_tasks, bool) else None
    )
    attempt_limit = (
        max_attempts
        if isinstance(max_attempts, int) and not isinstance(max_attempts, bool)
        else None
    )

    started_at = now_iso()
    outcomes: list[TaskExecutionOutcome] = []
    run_log = _SequentialRunLog()
    paths: Any = None

    try:
        scope_mode = _normalize_scope_mode(scope)
        verify_mode = _normalize_verify_mode(verify)
        if review_requested and not pr_requested:
            raise ForgeError("--review requires --pr.")
        api_key_override = _resolve_api_key_override(
            api_key=api_key,
            api_key_env=api_key_env,
            api_key_stdin=api_key_stdin,
        )
        binding = resolve_workspace_binding(
            path,
            create_if_missing=False,
            allow_broad_workspace=allow_broad_workspace,
            source=_path_binding_source(cli_ctx, path),
        )
        ensure_workspace_policy(
            binding,
            action=WorkspaceAction.FORGE_RUN,
            allow_broad_workspace=allow_broad_workspace,
        )
        try:
            paths = load_current_run_paths(binding.workspace_context.focus_path)
        except ForgeError as e:
            if "current_run.json" in str(e):
                raise _missing_swarm_run_error(binding=binding) from e
            raise
        events.set_run_id(paths.run_id)
        run_log.bind(paths)
        plan = load_plan(paths)

        # PR flow is the default because it is what makes per-task verification and a
        # per-task resume point possible. A workspace without a git HEAD cannot do it,
        # so an unstated default degrades loudly instead of failing; an explicit --pr
        # still fails, because silently ignoring a flag the user typed is worse.
        git_flow_available = bool(paths.has_head_commit)
        if pr_requested and not git_flow_available:
            # --review only exists inside git flow, so degrading would drop it too.
            # Both count as "the user typed it", and neither gets dropped silently.
            explicit_flag = "--pr" if pr_explicit else ("--review" if review_requested else None)
            if explicit_flag is not None:
                raise ForgeError(
                    f"{explicit_flag} needs a git repository with at least one commit; this "
                    f"workspace ({os.fspath(paths.root)}) has none. Commit once, or drop "
                    f"{explicit_flag}."
                )
            pr_requested = False
            review_requested = False
            console.print(
                "[yellow]No git HEAD in this workspace:[/yellow] running without git flow. "
                "Tasks execute in place, nothing is committed, and per-task verification "
                "does not run."
            )
            events.emit(
                EVENT_VERIFICATION_UNAVAILABLE,
                {
                    "scope": "run",
                    "policy": verify_mode,
                    "reason": (
                        "sequential run degraded to --no-pr: workspace has no git HEAD, so no "
                        "per-task verification gate runs"
                    ),
                    "blocking": False,
                },
            )

        # The base branch is pinned once for the whole run rather than re-derived per
        # task from the current branch. A failed task leaves the checkout on its own
        # branch, so re-deriving would make the next task branch off rejected work --
        # which is exactly what --keep-going would otherwise do.
        resolved_base_branch: str | None = (
            base_branch.strip() if isinstance(base_branch, str) and base_branch.strip() else None
        )
        if pr_requested and resolved_base_branch is None:
            try:
                resolved_base_branch = (current_branch(paths.root) or "").strip() or None
            except GitOpsError as e:
                raise ForgeError(f"could not determine the base branch for git flow: {e}") from e
            if resolved_base_branch is None:
                raise ForgeError(
                    "could not determine the base branch for git flow; pass --base-branch."
                )

        run_mutation_guard = acquire_swarm_mutation_guard(
            paths,
            mode="forge_run:cli",
            on_wait=lambda info: _print_forge_lock_wait_notice(console, info),
        )
        try:
            if bool(getattr(run_mutation_guard, "acquired_after_wait", False)):
                plan = load_plan(paths)
            reconciliation_result, _ = _reconcile_plan_for_paths(
                paths=paths,
                plan=plan,
                refresh_if_stale=True,
            )
            if reconciliation_result.changed:
                save_plan(paths, plan)
            if reconciliation_result.warnings:
                console.print("[yellow]Plan reconciliation warnings:[/yellow]")
                for warning in reconciliation_result.warnings:
                    console.print(f"- {warning}")
            validation_warnings = _validate_forge_plan_for_paths(paths, plan)
            _write_plan_validation_artifact(
                paths=paths,
                reconciliation_result=reconciliation_result,
                validation_warnings=validation_warnings,
            )
            repair_payload = plan_repair_event_payload(plan)
            readiness_message = _forge_no_execution_ready_tasks_message(plan)
            if readiness_message is not None:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": readiness_message,
                        "source": "execution_readiness",
                        "warnings": list(validation_warnings),
                        **repair_payload,
                    },
                )
                raise ForgeError(readiness_message)
            try:
                raise_for_execution_ready_plan(
                    plan,
                    retry_failed=retry_failed_requested,
                    retry_changes_requested=retry_changes_requested_requested,
                    retry_merge_conflicts=False,
                    only=only,
                )
            except PlannerFailedError as e:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": str(e),
                        "source": "plan_validation",
                        "failure_category": getattr(e, "failure_category", None),
                        **repair_payload,
                    },
                )
                err = ForgeError(str(e))
                err.failure_category = e.failure_category  # type: ignore[attr-defined]
                raise err from e

            projected = _projected_sequential_order(
                plan,
                retry_failed=retry_failed_requested,
                retry_changes_requested=retry_changes_requested_requested,
                max_attempts=attempt_limit,
                only_ids=only_ids,
                max_tasks=task_limit,
            )
            # A run boundary in the append-only log, so a second `forge run` against the
            # same plan reads as a second run rather than as more of the first.
            run_log.append(
                "run_started",
                run_id=paths.run_id,
                order=list(projected),
                git_flow=pr_requested,
                verify=verify_mode,
                scope=scope_mode,
                dry_run=dry_run_requested,
            )
            console.rule("[bold]forge run[/bold]")
            console.print(f"Run ID: {paths.run_id}")
            console.print(f"Workspace: {os.fspath(paths.root)}")
            console.print(
                f"Mode: sequential, in-checkout (no worktrees) | "
                f"git flow: {'on' if pr_requested else 'off'} | verify: {verify_mode} | "
                f"scope: {scope_mode}"
            )
            if pr_requested and resolved_base_branch:
                console.print(f"Base branch: {resolved_base_branch}")
            _print_sequential_order(console, plan, projected)

            if dry_run_requested:
                data = {
                    "dry_run": True,
                    "order": list(projected),
                    "task_count": len(projected),
                }
                events.run_completed(ok=True, exit_code=EXIT_OK, data=data)
                raise typer.Exit(code=EXIT_OK)

            # From here on the workspace has an owner on record. Anything that stops
            # this process without reaching the terminal transition below leaves the
            # pointer saying `running` with a dead owner, which the next command
            # reconciles to `interrupted`.
            _mark_run_status(
                paths,
                RUN_STATUS_RUNNING,
                reason=f"forge run started ({len(projected)} task(s) projected)",
                plan=plan,
                mode="forge_run:cli",
            )

            stopped_reason = "completed"
            attempted: set[str] = set()
            while True:
                if task_limit is not None and len(outcomes) >= task_limit:
                    stopped_reason = "max_tasks"
                    break
                candidate = _next_sequential_task(
                    plan,
                    retry_failed=retry_failed_requested,
                    retry_changes_requested=retry_changes_requested_requested,
                    max_attempts=attempt_limit,
                    only_ids=only_ids,
                    exclude_ids=attempted,
                )
                if candidate is None:
                    break
                task = candidate.task
                task_id = candidate.task_id
                attempted.add(task_id)

                run_cfg = clone_cfg(effective)
                run_cfg.model = resolve_model_for_role(
                    cfg=effective,
                    role=ROLE_CODING,
                    plan=plan,
                    prefer_context="forge",
                )
                verify_commands: list[str] = []
                verify_command_source: str | None = None
                if verify_mode != "off":
                    selection = resolve_authoritative_task_verify_command_selection(
                        cfg=effective,
                        verify_cmd=verify_cmd,
                        task=task,
                        root=paths.root,
                        plan_requirements=[
                            str(item).strip()
                            for item in (plan.get("requirements") or [])
                            if str(item).strip()
                        ],
                    )
                    verify_commands = list(selection.commands)
                    verify_command_source = selection.source
                    run_cfg.verify_commands = list(verify_commands)

                position = len(outcomes) + 1
                console.rule(
                    f"[bold]{position}. {task_id}[/bold] {str(task.get('title') or '').strip()}"
                )
                run_log.append(
                    "task_started",
                    task_id=task_id,
                    title=str(task.get("title") or ""),
                    position=position,
                )
                try:
                    outcome = execute_forge_task(
                        console=console,
                        events=events,
                        paths=paths,
                        plan=plan,
                        task=task,
                        task_id=task_id,
                        effective=effective,
                        run_cfg=run_cfg,
                        effective_mode=effective_mode,
                        scope_mode=scope_mode,
                        verify_mode=verify_mode,
                        verify_commands=verify_commands,
                        verify_command_source=verify_command_source,
                        verify_repair_budget=verify_repair_budget,
                        api_key_override=api_key_override,
                        yes=yes is True,
                        no_log=no_log is True,
                        max_steps_provided=max_steps_provided,
                        pr=pr_requested,
                        review=review_requested,
                        base_branch=resolved_base_branch,
                        keep_branch=keep_branch_requested,
                        auto_resolve_conflicts=auto_resolve_requested,
                    )
                except ForgeTaskExecutionError as e:
                    # The task could not be executed at all. That is a command error,
                    # and continuing would run later tasks on top of an unknown repo
                    # state, so the run ends here.
                    run_log.append(
                        "task_error",
                        task_id=task_id,
                        error=str(e),
                        kind=e.kind,
                    )
                    _write_sequential_summary(
                        paths=paths,
                        run_id=paths.run_id,
                        started_at=started_at,
                        outcomes=outcomes,
                        stopped_reason="task_error",
                        exit_code=EXIT_ERROR,
                        pr=pr_requested,
                        verify_mode=verify_mode,
                        scope_mode=scope_mode,
                        error=str(e),
                    )
                    err = ForgeError(str(e))
                    err.failure_category = e.kind  # type: ignore[attr-defined]
                    raise err from e

                outcomes.append(outcome)
                # Recorded after the fact, not before: `execute_forge_task` derives the
                # task's step budget from the count of *previous* attempts, so bumping
                # first would make every first attempt look like a retry -- and the whole
                # point of sharing the core is that `forge run` and `forge exec` give a
                # task the same budget.
                attempts = _bump_task_attempt(paths, plan, task)
                run_log.append(
                    "task_finished",
                    task_id=task_id,
                    status=outcome.status,
                    success=outcome.success,
                    summary=outcome.summary,
                    report=os.fspath(outcome.report_path),
                    attempts=attempts,
                )
                if not outcome.success and not keep_going_requested:
                    stopped_reason = "task_failed"
                    break

            remaining = _unfinished_task_ids(plan)
            terminal_status, terminal_reason = _terminal_run_status_for(
                plan=plan,
                any_failure=any(not item.success for item in outcomes),
            )
            _mark_run_status(paths, terminal_status, reason=terminal_reason)
        finally:
            _mark_run_interrupted_if_still_running(
                paths,
                reason="forge run exited without recording an outcome",
            )
            run_mutation_guard.release()
    except (ConfigError, ForgeError, GitOpsError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e),
            kind="forge_error",
            exit_code=EXIT_ERROR,
            data={"failure_category": getattr(e, "failure_category", None)},
        )
        raise typer.Exit(code=EXIT_ERROR) from e
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001
        # An unexpected exception is the command failing, not "tasks were rejected".
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e) or e.__class__.__name__,
            kind="exception",
            exit_code=EXIT_ERROR,
            data={"exception_type": e.__class__.__name__},
        )
        raise typer.Exit(code=EXIT_ERROR) from e

    # Exit 1 means "ran to completion, work not accepted". Tasks left unexecuted
    # because of --only or --max-tasks are a scope the caller chose, not rejected
    # work, so only an actual task failure moves the exit code.
    failed = [item for item in outcomes if not item.success]
    exit_code = EXIT_NOT_ACCEPTED if failed else EXIT_OK

    summary_payload = _write_sequential_summary(
        paths=paths,
        run_id=paths.run_id,
        started_at=started_at,
        outcomes=outcomes,
        stopped_reason=stopped_reason,
        exit_code=exit_code,
        pr=pr_requested,
        verify_mode=verify_mode,
        scope_mode=scope_mode,
        remaining=remaining,
    )
    _print_sequential_summary(console, summary_payload)
    _print_usage_summary_from_logs(
        console=console,
        title="Sequential Run Usage Summary",
        log_paths=sorted(paths.execution_logs_dir.glob("*.jsonl")),
    )
    events.run_completed(
        ok=exit_code == EXIT_OK, exit_code=exit_code, data={"outcome": summary_payload}
    )
    raise typer.Exit(code=exit_code)


def forge_plan_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_plan(*args, **kwargs)


def forge_swarm_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_swarm(*args, **kwargs)


def forge_exec_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_exec(*args, **kwargs)


def forge_run_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_run(*args, **kwargs)
