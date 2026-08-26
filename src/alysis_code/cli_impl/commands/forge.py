from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import typer

from ...config import ConfigError, clone_cfg, load_config
from ...forge import (
    ForgeError,
    attach_asset,
    find_task,
    load_current_run_paths,
    load_plan,
)
from ...forge_events import (
    EVENT_PLAN_INVALID,
    EVENT_REVIEW_RESULT,
    EVENT_RUN_STARTED,
    EXIT_ERROR,
    ForgeEventEmitter,
    machine_session,
)
from ...plan_repair import (
    PLAN_STATUS_DRAFT,
    plan_repair_event_payload,
    plan_repair_metadata,
    plan_status,
    plan_status_detail,
)
from ...review_gate import ReviewError, review_task
from . import _patchable
from ._shared import Mode, _console, _Table
from .forge_asset_view import forge_asset_view_count, forge_asset_view_entries


def _cli_module() -> Any:
    module = sys.modules.get("alysis_code.cli")
    if module is not None:
        return module
    from ... import cli

    return cli


forge_app = typer.Typer(
    add_completion=False,
    help=(
        "Forge commands. `forge run` executes a plan sequentially in this checkout and is the "
        "default path; `forge swarm` is the parallel optimization for independent tasks."
    ),
)

_MACHINE_CONTEXT_KEY = "alysis_forge_machine"

MACHINE_OPTION_HELP = (
    "Emit newline-delimited JSON events on stdout instead of human output. "
    "One JSON object per line; exactly one terminal event (run_completed or error)."
)


def _store_machine_flag(ctx: typer.Context | None, value: bool) -> None:
    if ctx is None or value is not True:
        return
    if not isinstance(getattr(ctx, "obj", None), dict):
        ctx.obj = {}
    ctx.obj[_MACHINE_CONTEXT_KEY] = True


def _machine_enabled(ctx: typer.Context | None, machine: bool) -> bool:
    """True when either `forge --machine <cmd>` or `forge <cmd> --machine` was used.

    Only a real ``True`` counts. These commands are also called directly as Python
    functions (for example from the interactive quick-action menu), where unfilled
    parameters arrive as truthy typer ``OptionInfo`` objects rather than booleans --
    that must not silently switch a human invocation into the machine protocol.
    """
    if machine is True:
        return True
    current: Any = ctx
    while current is not None:
        obj = getattr(current, "obj", None)
        if isinstance(obj, dict) and obj.get(_MACHINE_CONTEXT_KEY):
            return True
        current = getattr(current, "parent", None)
    return False


def _forge_error_exit(
    console: Any,
    events: ForgeEventEmitter,
    error: Exception,
    *,
    code: int = EXIT_ERROR,
    kind: str = "forge_error",
    data: dict[str, Any] | None = None,
) -> typer.Exit:
    """Report a Forge error on both surfaces and build the matching Exit."""
    console.print(f"[red]Forge error:[/red] {error}")
    events.error(message=str(error), kind=kind, exit_code=code, data=data)
    return typer.Exit(code=code)


def _run_lifecycle_snapshot(paths: Any) -> dict[str, Any]:
    """The run's lifecycle block, so consumers never infer "active" from its absence."""
    from ...forge import read_current_run_pointer
    from ...run_state import describe_run_status, pointer_status, status_is_resumable

    pointer = read_current_run_pointer(paths.root) or {}
    status = pointer_status(pointer)
    return {
        "run_status": status,
        "run_status_description": describe_run_status(status),
        "run_status_updated_at": pointer.get("status_updated_at"),
        "run_status_reason": pointer.get("status_reason"),
        "run_owner": pointer.get("run_owner"),
        "resumable": status_is_resumable(status),
    }


def _plan_snapshot(paths: Any, plan: dict[str, Any]) -> dict[str, Any]:
    tasks = plan.get("tasks") or []
    return {
        **_run_lifecycle_snapshot(paths),
        "run_id": paths.run_id,
        "run_dir": os.fspath(paths.run_dir),
        "plan_json": os.fspath(paths.plan_json_path),
        "plan_md": os.fspath(paths.plan_md_path),
        "project_goal": str(plan.get("project_goal") or "").strip() or None,
        "task_count": len(tasks),
        "tasks": [
            {
                "id": str(task.get("id", "")),
                "status": str(task.get("status", "")),
                "title": str(task.get("title", "")),
                "dependencies": [str(dep) for dep in (task.get("dependencies") or [])],
            }
            for task in tasks
        ],
        **plan_repair_event_payload(plan),
    }


@forge_app.callback()
def forge_main(
    ctx: typer.Context,
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Forge commands. Human output is the default; --machine switches to NDJSON."""
    _store_machine_flag(ctx, machine)


@forge_app.command("plan")
def forge_plan(
    ctx: typer.Context = None,
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
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.plan", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(EVENT_RUN_STARTED, {"command": "forge.plan", "path": os.fspath(path)})
        cli = _cli_module()
        cli._require_active_subscription_ready(model=None, base_url=None)
        from ..forge import forge_plan_impl

        return forge_plan_impl(
            cli,
            path,
            create_path,
            allow_broad_workspace,
            cli_ctx=ctx,
            events=events,
        )


@forge_app.command("attach")
def forge_attach(
    ctx: typer.Context = None,
    source_path: Path = typer.Argument(..., help="File path to attach."),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.attach", machine=_machine_enabled(ctx, machine)) as events:
        console = _console()
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.attach",
                "path": os.fspath(path),
                "source_path": os.fspath(source_path),
            },
        )
        if not events.enabled:
            typer.echo(
                "Deprecation: `alysis forge attach` is the legacy asset attachment flow.\n"
                "Use `/assets` from chat or `alysis forge assets add` for the new flow with\n"
                "LLM comprehension and per-task binding. The legacy command continues to work,\n"
                "and attached assets are migrated on the next plan load.",
                err=True,
            )
        try:
            paths, metadata = attach_asset(path, source_path)
        except ForgeError as e:
            raise _forge_error_exit(console, events, e) from e

        console.print(f"Attached to run: {paths.run_id}")
        console.print(f"- original: {metadata.get('original_path')}")
        console.print(f"- stored: {metadata.get('stored_path')}")
        console.print(f"- size: {metadata.get('size_bytes')} bytes")
        if metadata.get("text_copy_path"):
            console.print(f"- extracted text: {metadata.get('text_copy_path')}")

        events.set_run_id(paths.run_id)
        events.run_completed(
            ok=True,
            data={
                "deprecated": True,
                "asset": {
                    "original_path": metadata.get("original_path"),
                    "stored_path": metadata.get("stored_path"),
                    "size_bytes": metadata.get("size_bytes"),
                    "text_copy_path": metadata.get("text_copy_path"),
                },
            },
        )


@forge_app.command("show")
def forge_show(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.show", machine=_machine_enabled(ctx, machine)) as events:
        console = _console()
        events.emit(EVENT_RUN_STARTED, {"command": "forge.show", "path": os.fspath(path)})
        try:
            paths = load_current_run_paths(path)
            plan = load_plan(paths)
        except ForgeError as e:
            events.emit(EVENT_PLAN_INVALID, {"reason": str(e), "source": "plan_load"})
            raise _forge_error_exit(console, events, e) from e

        events.set_run_id(paths.run_id)
        console.rule("[bold]forge show[/bold]")
        goal = str(plan.get("project_goal") or "").strip() or "(not set)"
        console.print(f"Run ID: {paths.run_id}")
        console.print(f"Project goal: {goal}")

        status = plan_status(plan)
        if status == PLAN_STATUS_DRAFT:
            console.print("[yellow]Plan status:[/yellow] draft (not execution-ready)")
            for reason in plan_status_detail(plan)["blocking_reasons"][:5]:
                console.print(f"- {reason}")
        else:
            console.print("Plan status: execution_ready")

        # A plan the host had to salvage says so, with the fields it touched.
        repair = plan_repair_metadata(plan)
        if repair["host_repaired"] or repair["forced_draft"]:
            labels = []
            if repair["host_repaired"]:
                labels.append("host-repaired")
            if repair["forced_draft"]:
                labels.append("forced draft after clarification cap")
            console.print("[yellow]Plan repairs:[/yellow] " + ", ".join(labels))
            if repair["host_repaired_fields"]:
                console.print(
                    "Host-repaired fields: " + ", ".join(repair["host_repaired_fields"][:10])
                )

        tasks = plan.get("tasks") or []
        task_table = _Table(title="Tasks")
        task_table.add_column("id")
        task_table.add_column("status")
        task_table.add_column("title")
        task_table.add_column("dependencies")
        if tasks:
            for task in tasks:
                deps = task.get("dependencies") or []
                task_table.add_row(
                    str(task.get("id", "")),
                    str(task.get("status", "")),
                    str(task.get("title", "")),
                    ", ".join(str(d) for d in deps) if deps else "-",
                )
        console.print(task_table)

        assets = forge_asset_view_entries(paths, plan)
        asset_table = _Table(title="Assets")
        asset_table.add_column("source")
        asset_table.add_column("stored_path")
        asset_table.add_column("size_bytes")
        if assets:
            for asset in assets:
                asset_table.add_row(
                    asset.source,
                    asset.stored_path,
                    "" if asset.size_bytes is None else str(asset.size_bytes),
                )
        console.print(asset_table)
        if assets:
            names = [asset.display_name for asset in assets if asset.display_name]
            if names:
                console.print(f"Asset files: {', '.join(names)}")

        snapshot = _plan_snapshot(paths, plan)
        snapshot["assets"] = [
            {
                "source": asset.source,
                "stored_path": asset.stored_path,
                "size_bytes": asset.size_bytes,
                "display_name": asset.display_name,
            }
            for asset in assets
        ]
        events.run_completed(ok=True, data=snapshot)


@forge_app.command("status")
def forge_status(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.status", machine=_machine_enabled(ctx, machine)) as events:
        console = _console()
        events.emit(EVENT_RUN_STARTED, {"command": "forge.status", "path": os.fspath(path)})
        try:
            paths = load_current_run_paths(path)
            plan = load_plan(paths)
        except ForgeError as e:
            events.emit(EVENT_PLAN_INVALID, {"reason": str(e), "source": "plan_load"})
            raise _forge_error_exit(console, events, e) from e

        events.set_run_id(paths.run_id)
        asset_count = forge_asset_view_count(paths, plan)
        table = _Table(title="forge status")
        table.add_column("field")
        table.add_column("value")
        table.add_row("run_id", paths.run_id)
        table.add_row("run_dir", os.fspath(paths.run_dir))
        table.add_row("plan_json", os.fspath(paths.plan_json_path))
        table.add_row("plan_md", os.fspath(paths.plan_md_path))
        table.add_row("tasks", str(len(plan.get("tasks") or [])))
        table.add_row("assets", str(asset_count))
        lifecycle = _run_lifecycle_snapshot(paths)
        table.add_row(
            "run_status",
            f"{lifecycle['run_status']} ({lifecycle['run_status_description']})",
        )
        console.print(table)
        if lifecycle["resumable"]:
            console.print(
                "This run stopped before finishing. Continue it with: alysis forge resume --path ."
            )

        snapshot = _plan_snapshot(paths, plan)
        snapshot["asset_count"] = asset_count
        events.run_completed(ok=True, data=snapshot)


@forge_app.command("review")
def forge_review(
    ctx: typer.Context = None,
    task_id: str = typer.Argument(..., help="Task id from plan.json (for example T01)."),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
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
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.review", machine=_machine_enabled(ctx, machine)) as events:
        console = _console()
        events.emit(
            EVENT_RUN_STARTED,
            {"command": "forge.review", "path": os.fspath(path), "task_id": task_id},
        )
        _cli_module()._require_active_subscription_ready(model=model, base_url=base_url)
        cfg = _patchable("load_config", load_config)()
        effective = clone_cfg(cfg)
        if base_url is not None:
            effective.base_url = base_url
        if model is not None:
            effective.model = model
        if temperature is not None:
            _cli_module()._apply_temperature_override(effective, temperature)

        try:
            if not effective.model:
                raise ConfigError("Model is not set. Run: alysis config set model <MODEL>")
            api_key_override = _cli_module()._resolve_api_key_override(
                api_key=api_key,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
            )
            paths = load_current_run_paths(path)
            events.set_run_id(paths.run_id)
            plan = load_plan(paths)
            task = find_task(plan, task_id)
            if task is None:
                raise ForgeError(f"Task not found: {task_id}")
            outcome = _patchable("review_task", review_task)(
                paths=paths,
                plan=plan,
                task=task,
                cfg=effective,
                api_key_override=api_key_override,
            )
        except (ConfigError, ForgeError, ReviewError) as e:
            raise _forge_error_exit(console, events, e, data={"task_id": task_id}) from e

        console.print(f"Task: {task_id} ({task.get('title', '')})")
        console.print(f"Approved: {'yes' if outcome.approved else 'no'}")
        console.print(f"Confidence: {outcome.confidence}")
        console.print(f"Summary: {outcome.summary}")
        console.print(f"Review JSON: {outcome.json_path}")
        console.print(f"Review Markdown: {outcome.markdown_path}")

        review_payload = {
            "task_id": task_id,
            "title": str(task.get("title", "")),
            "approved": bool(outcome.approved),
            "confidence": outcome.confidence,
            "summary": outcome.summary,
            "blocking_issues": outcome.blocking_issues_count,
            "non_blocking_issues": outcome.non_blocking_issues_count,
            "review_json": os.fspath(outcome.json_path),
            "review_markdown": os.fspath(outcome.markdown_path),
        }
        events.emit(EVENT_REVIEW_RESULT, review_payload)
        # A review that rejects work is a review that did its job. Exit 0 and let the
        # approved flag carry the verdict; nonzero stays reserved for command errors.
        events.run_completed(ok=True, data={"review": review_payload})


@forge_app.command("run")
def forge_run(
    ctx: typer.Context = None,
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
            "(default). --no-pr leaves changes uncommitted and runs no verification gate, "
            "so use it only where git flow is unavailable."
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
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Execute every ready task sequentially, in dependency order, in this checkout.

    This is the default way to execute a Forge plan. One task at a time, no
    worktrees, no batch integration gate: each task branches, commits, verifies
    and merges in the main checkout before the next one starts, so a failure
    stops the run at a state you can inspect and resume from.

    Use `forge swarm` instead when independent tasks should run in parallel and
    the extra machinery (worktrees, batch integration gate, conflict resolution)
    is worth it.
    """
    with machine_session("forge.run", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.run",
                "path": os.fspath(path),
                "parallel": 1,
                "worktrees": False,
                "dry_run": dry_run,
                "only": only,
                "max_tasks": max_tasks,
                "pr": pr,
                "review": review,
                "verify": verify,
            },
        )
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ..forge import forge_run_impl

        return forge_run_impl(
            cli,
            path,
            allow_broad_workspace,
            only,
            max_tasks,
            max_attempts,
            retry_failed,
            retry_changes_requested,
            keep_going,
            dry_run,
            scope,
            verify,
            verify_cmd,
            verify_repair_attempts,
            pr,
            review,
            base_branch,
            keep_branch,
            auto_resolve_conflicts,
            mode,
            model,
            base_url,
            temperature,
            stream,
            max_steps,
            no_log,
            api_key_env,
            api_key_stdin,
            api_key,
            yes,
            cli_ctx=ctx,
            events=events,
        )


@forge_app.command("unlock")
def forge_unlock(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Clear the lock even when the owner is not provably gone. Use only when you "
            "know that process is dead: two concurrent executions can corrupt the run."
        ),
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Inspect this workspace's Forge locks and clear the ones that are safe to clear.

    Exit 0 when the workspace is unlocked (nothing was there, or everything stale was
    cleared). Exit 1 when a lock was kept because its owner could still be alive --
    that is a reportable refusal, not a command failure.
    """
    with machine_session("forge.unlock", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {"command": "forge.unlock", "path": os.fspath(path), "force": force is True},
        )
        from ..forge_recovery import forge_unlock_impl

        return forge_unlock_impl(path=path, force=force, events=events)


@forge_app.command("resume")
def forge_resume(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    reapprove: bool = typer.Option(
        False,
        "--reapprove",
        help=(
            "Accept the current plan when it drifted since the run was approved. "
            "Without it, a drifted plan stops the resume and lists what changed."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be resumed and exit without executing anything.",
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
    keep_going: bool = typer.Option(
        False,
        "--keep-going",
        help="Continue with the next independent task after a failure instead of stopping.",
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
        help="How many times a failing strict verification is fed back to the agent.",
    ),
    pr: bool = typer.Option(
        True,
        "--pr/--no-pr",
        help="Run each task as a branch/commit/verify/merge cycle in the main checkout.",
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
        help="On merge conflict, start a resolver agent instead of stopping with a report.",
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
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Continue an interrupted run from its last incomplete task.

    Re-checks the plan against the fingerprint recorded when the run started. A plan
    that drifted stops the resume and lists exactly what changed; `--reapprove`
    accepts the current plan and continues. Execution itself is `forge run`, with
    retries enabled for the statuses an interruption leaves behind.
    """
    with machine_session("forge.resume", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.resume",
                "path": os.fspath(path),
                "reapprove": reapprove is True,
                "dry_run": dry_run is True,
            },
        )
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ..forge_recovery import forge_resume_impl

        return forge_resume_impl(
            cli,
            path=path,
            reapprove=reapprove,
            dry_run=dry_run,
            events=events,
            allow_broad_workspace=allow_broad_workspace,
            only=only,
            max_tasks=max_tasks,
            max_attempts=max_attempts,
            keep_going=keep_going,
            scope=scope,
            verify=verify,
            verify_cmd=verify_cmd,
            verify_repair_attempts=verify_repair_attempts,
            pr=pr,
            review=review,
            base_branch=base_branch,
            keep_branch=keep_branch,
            auto_resolve_conflicts=auto_resolve_conflicts,
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
            cli_ctx=ctx,
        )


@forge_app.command("swarm")
def forge_swarm(
    ctx: typer.Context = None,
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
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Execute independent ready tasks in parallel, each in its own git worktree.

    This is the parallel optimization, not the default way to execute a plan. It
    brings machinery `forge run` does not have -- per-task worktrees, a batch
    integration gate, and agent-driven merge-conflict resolution -- so it pays off
    only when the plan really does have independent tasks with non-overlapping
    write scopes.

    For a dependency chain, or when you want one linear log and a stop-and-inspect
    failure, use `forge run`. `--parallel 1` delegates there automatically.
    """
    with machine_session("forge.swarm", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.swarm",
                "path": os.fspath(path),
                "parallel": parallel,
                "dry_run": dry_run,
                "only": only,
                "max_tasks": max_tasks,
                "review": review,
                "verify": verify,
            },
        )
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ..forge import forge_swarm_impl

        return forge_swarm_impl(
            cli,
            path,
            allow_broad_workspace,
            parallel,
            base_branch,
            max_tasks,
            max_attempts,
            dry_run,
            keep_worktrees,
            retry_failed,
            retry_changes_requested,
            only,
            retry_merge_conflicts,
            scope,
            verify,
            verify_cmd,
            integration_verify,
            integration_verify_cmd,
            replan,
            review,
            mode,
            model,
            base_url,
            temperature,
            stream,
            max_steps,
            no_log,
            api_key_env,
            api_key_stdin,
            api_key,
            yes,
            cli_ctx=ctx,
            events=events,
        )


@forge_app.command("exec")
def forge_exec(
    ctx: typer.Context = None,
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
            "On merge conflict, start a resolver agent in a dedicated worktree instead of "
            "stopping with a conflict report. Off by default: the sequential path reports "
            "the conflict and how to land it."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Execute one plan task in this checkout.

    Same execution core as `forge run`, which is just this applied to every ready
    task in dependency order. Use `forge run` to execute a whole plan.
    """
    with machine_session("forge.exec", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.exec",
                "path": os.fspath(path),
                "task_id": task_id,
                "pr": pr,
                "review": review,
                "verify": verify,
            },
        )
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ..forge import forge_exec_impl

        return forge_exec_impl(
            cli,
            task_id,
            path,
            mode,
            model,
            base_url,
            temperature,
            stream,
            max_steps,
            no_log,
            api_key_env,
            api_key_stdin,
            api_key,
            pr,
            review,
            base_branch,
            keep_branch,
            scope,
            verify,
            verify_cmd,
            yes,
            verify_repair_attempts=verify_repair_attempts,
            auto_resolve_conflicts=auto_resolve_conflicts,
            events=events,
        )
