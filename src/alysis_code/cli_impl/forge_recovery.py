"""Getting a workspace un-stuck after an execution died: `forge unlock`, `forge resume`.

An interrupted run leaves two kinds of residue, and each needs its own answer.

*The lock* outlives the process that took it. ``forge unlock`` reads it, explains
the staleness verdict in words, and clears it when -- and only when -- the owner is
provably gone. ``--force`` exists for the genuinely ambiguous case (another host,
an unprobeable pid), and says plainly that the operator, not the tool, is the one
asserting the owner is dead.

*The run pointer* outlives it too. ``forge resume`` picks a run back up from where
it stopped: it re-checks the plan against the fingerprint recorded when the run
started, and if the plan moved in the meantime it says exactly what moved and
stops for re-approval rather than either silently continuing or silently throwing
the approval away.

Both commands are pure recovery. Neither invents work: unlock only deletes lock
files, and resume delegates the actual execution to ``forge run`` once it has
established that resuming is safe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer

from ..forge import (
    ForgeError,
    current_run_pointer_path,
    load_current_run_paths,
    load_plan,
    read_current_run_pointer,
    reconcile_current_run_status,
    save_plan,
    set_current_run_status,
)
from ..forge_events import (
    EXIT_ERROR,
    EXIT_NOT_ACCEPTED,
    EXIT_OK,
    ForgeEventEmitter,
)
from ..run_lock import (
    STALENESS_STALE,
    clear_run_mutation_lock,
    describe_run_mutation_lock,
)
from ..run_state import (
    RESUMABLE_RUN_STATUSES,
    RUN_STATUS_APPROVED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_RUNNING,
    compare_plan_fingerprints,
    describe_run_status,
    plan_fingerprint,
    pointer_status,
)
from ..workspace_binding import (
    WorkspaceAction,
    WorkspaceBindingError,
    ensure_workspace_policy,
    resolve_workspace_binding,
)
from .commands._shared import _console

_WORKSPACE_LOCK_DIR_NAME = "workspace_execution"


def _events_or_null(events: Any) -> ForgeEventEmitter:
    if isinstance(events, ForgeEventEmitter):
        return events
    return ForgeEventEmitter(command="forge", enabled=False)


def _lock_targets(path: Path) -> tuple[Path, list[tuple[str, Path]], Any]:
    """Locate the workspace lock and, when resolvable, the current run's lock.

    The workspace lock is found from the path alone, on purpose: a workspace whose
    ``current_run.json`` is missing or corrupt is exactly the workspace most likely
    to be stuck, and it must still be unlockable.
    """
    binding = resolve_workspace_binding(path, create_if_missing=False, source="explicit_path")
    root = binding.workspace_context.workspace_root
    paths = None
    try:
        paths = load_current_run_paths(binding.workspace_context.focus_path)
    except ForgeError:
        paths = None
    runtime_dir = paths.runtime_dir if paths is not None else root / ".alysis"
    # Workspace lock first: it is the one that blocks every command, and the one that
    # still exists when there is no resolvable run to report a run lock for.
    targets: list[tuple[str, Path]] = [("workspace", runtime_dir / _WORKSPACE_LOCK_DIR_NAME)]
    if paths is not None:
        targets.append((f"run {paths.run_id}", paths.run_dir))
    return root, targets, paths


def _print_lock_report(console: Any, report: dict[str, Any]) -> None:
    staleness = report.get("staleness") or {}
    lock = report.get("lock") or {}
    verdict = str(staleness.get("verdict") or "unknown")
    console.print(f"[bold]{report.get('label')}[/bold] lock: {verdict}")
    console.print(f"  reason: {staleness.get('reason') or '(none recorded)'}")
    for label, key in (
        ("owner", "owner_id"),
        ("host", "hostname"),
        ("pid", "pid"),
        ("mode", "mode"),
    ):
        # The verdict carries these when it could parse them; the raw lock is the
        # fallback for a lock too malformed to assess but still worth describing.
        value = staleness.get(key) or lock.get(key)
        if value not in (None, ""):
            console.print(f"  {label}: {value}")
    for label, key in (("age", "age_s"), ("heartbeat age", "heartbeat_age_s")):
        seconds = staleness.get(key)
        if isinstance(seconds, (int, float)):
            console.print(f"  {label}: {_format_seconds(float(seconds))}")
    console.print(f"  file: {report.get('lock_path')}")


def _format_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def forge_unlock(
    path: Path = typer.Option(Path("."), "--path"),
    force: bool = typer.Option(False, "--force"),
    events: Any = None,
) -> None:
    """Inspect the workspace's Forge locks and clear the ones that are safe to clear."""
    events = _events_or_null(events)
    console = _console()
    forced = force is True

    try:
        root, targets, paths = _lock_targets(path)
    except (ForgeError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(message=str(e), kind="forge_error", exit_code=EXIT_ERROR)
        raise typer.Exit(code=EXIT_ERROR) from e

    if paths is not None:
        events.set_run_id(paths.run_id)

    console.rule("[bold]forge unlock[/bold]")
    console.print(f"Workspace: {os.fspath(root)}")

    reports: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    cleared: list[dict[str, Any]] = []

    for label, run_dir in targets:
        present = describe_run_mutation_lock(run_dir, label=label)
        if present is None:
            console.print(f"[bold]{label}[/bold] lock: none")
            reports.append({"label": label, "present": False, "cleared": False})
            continue
        _print_lock_report(console, present)
        verdict = str((present.get("staleness") or {}).get("verdict") or "")
        if verdict != STALENESS_STALE and not forced:
            console.print(
                "  [yellow]kept:[/yellow] this lock is not provably stale. Wait for the "
                "owner to finish, or re-run with --force if you are certain that process "
                "is gone."
            )
            blocked.append(present)
            reports.append({**present, "present": True, "cleared": False, "forced": False})
            continue
        result = clear_run_mutation_lock(run_dir, force=forced)
        if result.get("cleared"):
            if result.get("forced"):
                console.print(
                    "  [yellow]force-cleared:[/yellow] the owner was not provably gone. "
                    "If it is still alive, two executions can now mutate this workspace "
                    "at once."
                )
            else:
                console.print("  [green]cleared:[/green] owner was provably gone.")
            cleared.append(result)
        reports.append(result)

    # A cleared lock is only half the recovery: the pointer may still claim `running`.
    status_after = None
    if root is not None:
        try:
            reconcile_current_run_status(root)
            status_after = pointer_status(read_current_run_pointer(root))
        except Exception:  # noqa: BLE001 - reporting only
            status_after = None

    if status_after is not None:
        console.print(f"Run status: {status_after} ({describe_run_status(status_after)})")
        if status_after in RESUMABLE_RUN_STATUSES:
            console.print("Continue this run with: alysis forge resume --path .")

    exit_code = EXIT_NOT_ACCEPTED if blocked else EXIT_OK
    if not cleared and not blocked:
        console.print("Nothing to clear.")
    events.run_completed(
        ok=exit_code == EXIT_OK,
        exit_code=exit_code,
        data={
            "workspace_root": os.fspath(root),
            "forced": forced,
            "locks": reports,
            "cleared_count": len(cleared),
            "blocked_count": len(blocked),
            "run_status": status_after,
        },
    )
    raise typer.Exit(code=exit_code)


def _in_flight_task_ids(plan: dict[str, Any]) -> list[str]:
    """Task ids the dead process left mid-execution."""
    return sorted(
        str(task.get("id") or "")
        for task in plan.get("tasks") or []
        if isinstance(task, dict)
        and str(task.get("status") or "").strip().lower() == "in_progress"
        and str(task.get("id") or "")
    )


def _reset_in_flight_tasks(paths: Any, plan: dict[str, Any]) -> list[str]:
    """Move tasks the dead process left ``in_progress`` back to a runnable status.

    ``in_progress`` is not a runnable status -- by design, so a live run cannot pick
    up its own task twice. That also means a killed run's task would be skipped
    forever, so resume rewrites it to ``interrupted``, which *is* runnable and which
    records what happened rather than pretending the task was never started.
    """
    reset: list[str] = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("status") or "").strip().lower() != "in_progress":
            continue
        # The *task* status vocabulary, which happens to share this word with the run
        # lifecycle enum. `swarm_scheduler._runnable_status` treats it as runnable.
        task["status"] = "interrupted"
        reset.append(str(task.get("id") or ""))
    if reset:
        save_plan(paths, plan)
    return reset


def forge_resume(
    cli_mod: Any,
    *,
    path: Path,
    reapprove: bool = False,
    dry_run: bool = False,
    events: Any = None,
    **run_kwargs: Any,
) -> None:
    """Continue an interrupted run from its last incomplete task."""
    events = _events_or_null(events)
    console = _console()
    reapprove_requested = reapprove is True

    try:
        binding = resolve_workspace_binding(
            path,
            create_if_missing=False,
            allow_broad_workspace=bool(run_kwargs.get("allow_broad_workspace")),
            source="explicit_path",
        )
        ensure_workspace_policy(
            binding,
            action=WorkspaceAction.FORGE_RUN,
            allow_broad_workspace=bool(run_kwargs.get("allow_broad_workspace")),
        )
        # Loading the pointer is what reconciles a crash leftover, so the status read
        # below is the post-crash truth rather than the dead process's last claim.
        paths = load_current_run_paths(binding.workspace_context.focus_path)
        plan = load_plan(paths)
    except (ForgeError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(message=str(e), kind="forge_error", exit_code=EXIT_ERROR)
        raise typer.Exit(code=EXIT_ERROR) from e

    events.set_run_id(paths.run_id)
    pointer = read_current_run_pointer(paths.root)
    status = pointer_status(pointer)

    console.rule("[bold]forge resume[/bold]")
    console.print(f"Run ID: {paths.run_id}")
    console.print(f"Status: {status} ({describe_run_status(status)})")
    reason = str((pointer or {}).get("status_reason") or "").strip()
    if reason:
        console.print(f"[dim]{reason}[/dim]")

    blocker = _resume_blocker(status)
    if blocker is not None:
        console.print(f"[red]Cannot resume:[/red] {blocker}")
        events.error(
            message=blocker,
            kind="forge_error",
            exit_code=EXIT_ERROR,
            data={"run_status": status, "run_id": paths.run_id},
        )
        raise typer.Exit(code=EXIT_ERROR)

    # `--dry-run` reports and exits, so from here on nothing may be written until it
    # has had its say -- including the re-approval and the in-flight task re-arming.
    dry = dry_run is True

    current_fingerprint = plan_fingerprint(plan)
    drift = compare_plan_fingerprints((pointer or {}).get("plan_fingerprint"), current_fingerprint)
    if drift.changed:
        console.print("[yellow]The plan changed since this run was approved:[/yellow]")
        for line in drift.reasons:
            console.print(f"- {line}")
        if not reapprove_requested:
            message = (
                "the plan drifted since this run started, so resuming would execute work "
                "that was never approved. Review the changes above, then re-run with "
                "--reapprove to accept the current plan and continue."
            )
            console.print(f"[red]Cannot resume:[/red] {message}")
            # Deliberately not demoted to draft: the operator has not rejected this
            # plan, they have not looked at it yet. Throwing the approval away here
            # would make "I looked and it is fine" cost a full re-plan.
            events.error(
                message=message,
                kind="plan_drift",
                exit_code=EXIT_NOT_ACCEPTED,
                data={
                    "run_status": status,
                    "run_id": paths.run_id,
                    "drift": drift.to_json(),
                },
            )
            raise typer.Exit(code=EXIT_NOT_ACCEPTED)
        if dry:
            console.print("Would re-approve the current plan and continue.")
        else:
            console.print("[green]Re-approved:[/green] continuing against the current plan.")
            with_fingerprint = set_current_run_status(
                paths,
                RUN_STATUS_APPROVED,
                reason="plan re-approved at resume after drift",
                plan=plan,
            )
            if with_fingerprint is None:
                console.print(
                    "[yellow]Note:[/yellow] could not update "
                    f"{current_run_pointer_path(paths.root)}; the run will still resume."
                )

    in_flight = _in_flight_task_ids(plan)
    if in_flight:
        verb = "would be re-armed for retry" if dry else "now retryable"
        console.print(
            f"Tasks left mid-flight by the interrupted run, {verb}: " + ", ".join(in_flight)
        )
    if not dry:
        _reset_in_flight_tasks(paths, plan)

    # Same definition of "outstanding" the sequential runner reports, so resume and
    # `forge run`'s summary can never disagree about what is left.
    from .forge import _unfinished_task_ids

    remaining = _unfinished_task_ids(plan)
    if not remaining:
        console.print("Nothing left to execute." + ("" if dry else " Marking the run completed."))
        if not dry:
            set_current_run_status(
                paths,
                RUN_STATUS_COMPLETED,
                reason="resume found every task already in a terminal successful status",
            )
        events.run_completed(
            ok=True,
            exit_code=EXIT_OK,
            data={
                "run_id": paths.run_id,
                "resumed": False,
                "remaining": [],
                "dry_run": dry,
            },
        )
        raise typer.Exit(code=EXIT_OK)

    verb = "Would resume" if dry else "Resuming"
    console.print(f"{verb} {len(remaining)} unfinished task(s): {', '.join(remaining)}")
    if dry:
        events.run_completed(
            ok=True,
            exit_code=EXIT_OK,
            data={
                "run_id": paths.run_id,
                "resumed": False,
                "remaining": remaining,
                "dry_run": True,
            },
        )
        raise typer.Exit(code=EXIT_OK)

    from .forge import forge_run_impl

    # `forge run` is the execution engine; resume's whole job was to establish that
    # running it now is safe. Retry flags are on because the statuses resume exists
    # to recover from -- interrupted, failed, changes_requested -- are precisely the
    # ones a plain `forge run` skips.
    return forge_run_impl(
        cli_mod,
        path,
        run_kwargs.get("allow_broad_workspace", False),
        run_kwargs.get("only"),
        run_kwargs.get("max_tasks"),
        run_kwargs.get("max_attempts"),
        True,
        True,
        run_kwargs.get("keep_going", False),
        False,
        run_kwargs.get("scope", "strict"),
        run_kwargs.get("verify", "warn"),
        run_kwargs.get("verify_cmd"),
        run_kwargs.get("verify_repair_attempts"),
        run_kwargs.get("pr", True),
        run_kwargs.get("review", False),
        run_kwargs.get("base_branch"),
        run_kwargs.get("keep_branch", False),
        run_kwargs.get("auto_resolve_conflicts", False),
        run_kwargs.get("mode"),
        run_kwargs.get("model"),
        run_kwargs.get("base_url"),
        run_kwargs.get("temperature"),
        run_kwargs.get("stream"),
        run_kwargs.get("max_steps"),
        run_kwargs.get("no_log", False),
        run_kwargs.get("api_key_env"),
        run_kwargs.get("api_key_stdin", False),
        run_kwargs.get("api_key"),
        run_kwargs.get("yes", False),
        cli_ctx=run_kwargs.get("cli_ctx"),
        events=events,
    )


def _resume_blocker(status: str) -> str | None:
    if status in RESUMABLE_RUN_STATUSES:
        return None
    if status == RUN_STATUS_RUNNING:
        return (
            "this run is still marked running and something holds the workspace lock. "
            "Wait for it to finish, or inspect the lock with `alysis forge unlock`."
        )
    if status == RUN_STATUS_COMPLETED:
        return "this run already completed; start a new one with `alysis forge plan`."
    if status == RUN_STATUS_APPROVED:
        return (
            "this run was never interrupted. Execute it with `alysis forge run` "
            "instead of resuming."
        )
    if status == RUN_STATUS_DRAFT:
        return (
            "this run's plan is still a draft, so there is nothing to resume. Finish "
            "planning first."
        )
    return f"unexpected run status: {status}"


def forge_unlock_impl(*args: Any, **kwargs: Any) -> Any:
    return forge_unlock(*args, **kwargs)


def forge_resume_impl(cli_mod: Any, **kwargs: Any) -> Any:
    from .forge import _sync_cli_globals

    _sync_cli_globals(cli_mod)
    return forge_resume(cli_mod, **kwargs)
