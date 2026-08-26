from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from ...config import ConfigError, load_config
from ...hooks import (
    canonicalize_hook_event_name,
    hook_audit_artifact_path,
    is_project_hooks_config_trusted,
    load_hook_config_file,
    load_resolved_hooks_config,
    load_trust_state,
    project_hooks_config_path,
    project_local_hooks_config_path,
    read_hook_audit_events,
    trust_project_hooks_config,
    untrust_project_hooks_config,
    user_hooks_config_path,
)
from ...runtime_kind import RuntimeKind, normalize_runtime_kind
from ...session_store import filter_sessions_to_local_owner, list_sessions, resolve_sessions_dir
from ...surface.styles import STYLE_CONTENT
from . import _patchable
from ._shared import _console, _resolve_tool_workspace_root, _Table

hooks_app = typer.Typer(
    add_completion=False,
    help="Lifecycle hook inspection, trust, toggle, and audit commands.",
)


def _project_hooks_config_or_exit(
    *,
    path: Path,
    console: Any,
    validate: bool = True,
) -> tuple[Path, Path]:
    workspace_root = _resolve_tool_workspace_root(path=path)
    config_path = project_hooks_config_path(workspace_root)
    if not config_path.exists():
        console.print(f"[red]Project hooks config not found:[/red] {config_path}")
        raise typer.Exit(code=1)
    if validate:
        try:
            load_hook_config_file(config_path)
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    return workspace_root, config_path


@dataclass(frozen=True)
class _HookSourceStatus:
    source_scope: str
    path: Path
    exists: bool
    trusted: bool
    status: str
    event_count: int = 0
    hook_count: int = 0
    issue: str = ""


def _collect_hook_source_statuses(*, workspace_root: Path) -> list[_HookSourceStatus]:
    trust_state = load_trust_state()
    candidate_paths = (
        ("user", user_hooks_config_path()),
        ("project", project_hooks_config_path(workspace_root)),
        ("project_local", project_local_hooks_config_path(workspace_root)),
    )
    statuses: list[_HookSourceStatus] = []
    for source_scope, hook_config_path in candidate_paths:
        if not hook_config_path.exists():
            statuses.append(
                _HookSourceStatus(
                    source_scope=source_scope,
                    path=hook_config_path,
                    exists=False,
                    trusted=(source_scope != "project"),
                    status="missing",
                )
            )
            continue
        trusted = source_scope != "project" or is_project_hooks_config_trusted(
            workspace_root=workspace_root,
            config_path=hook_config_path,
            state=trust_state,
        )
        try:
            config_file = load_hook_config_file(hook_config_path)
        except ConfigError as exc:
            statuses.append(
                _HookSourceStatus(
                    source_scope=source_scope,
                    path=hook_config_path,
                    exists=True,
                    trusted=trusted,
                    status="invalid",
                    issue=str(exc),
                )
            )
            continue
        event_count = len(config_file.hooks)
        hook_count = sum(
            len(group.hooks) for groups in config_file.hooks.values() for group in groups
        )
        statuses.append(
            _HookSourceStatus(
                source_scope=source_scope,
                path=hook_config_path,
                exists=True,
                trusted=trusted,
                status="loaded" if trusted or source_scope != "project" else "untrusted",
                event_count=event_count,
                hook_count=hook_count,
            )
        )
    return statuses


_HOOK_TOOL_EVENTS = {"PreToolUse", "PostToolUse"}
_HOOK_SESSION_SOURCES = {"startup", "resume", "fork"}


def _hook_test_match_result(
    *,
    event_name: str,
    matcher_target: str,
    runtime_kind: str,
    session_source: str,
    matcher: str,
    hook: Any,
) -> tuple[bool, str]:
    if hook.runtime_kinds and runtime_kind not in set(hook.runtime_kinds):
        return False, "runtime_kind mismatch"
    if hook.session_source and session_source not in set(hook.session_source):
        return False, "session_source mismatch"
    if event_name in _HOOK_TOOL_EVENTS:
        if not matcher_target:
            return False, "missing tool target"
        if matcher:
            try:
                if re.search(matcher, matcher_target) is None:
                    return False, "matcher miss"
            except re.error as exc:
                return False, f"invalid matcher: {exc}"
    return True, "matched"


@hooks_app.command("list")
def hooks_list(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    entries: list[tuple[str, str, str, str, str, str, str, str, str, str, str]] = []
    for status in _collect_hook_source_statuses(workspace_root=workspace_root):
        if not status.exists or status.status == "invalid":
            continue
        try:
            config_file = load_hook_config_file(status.path)
        except ConfigError:
            continue
        for event_name, groups in config_file.hooks.items():
            for group in groups:
                if not group.enabled:
                    continue
                for hook in sorted(
                    (item for item in group.hooks if item.enabled),
                    key=lambda item: (-item.priority, item.id or "", item.command),
                ):
                    entries.append(
                        (
                            status.source_scope,
                            status.status,
                            "trusted" if status.trusted else "untrusted",
                            event_name,
                            group.matcher or "*",
                            hook.id or "-",
                            str(hook.priority),
                            hook.failure_policy,
                            ",".join(hook.runtime_kinds or ()) or "-",
                            ",".join(hook.session_source or ()) or "-",
                            hook.command,
                        )
                    )
    table = _Table(title=f"Hooks ({len(entries)})")
    table.add_column("scope")
    table.add_column("status")
    table.add_column("trust")
    table.add_column("event")
    table.add_column("matcher")
    table.add_column("id")
    table.add_column("priority", justify="right")
    table.add_column("failure")
    table.add_column("runtime_kinds")
    table.add_column("session_source")
    table.add_column("command")
    for row in entries:
        table.add_row(*row)
    console.print(table)
    for row in entries:
        if row[5] != "-":
            console.print(
                f"[dim]hook:[/dim] event={row[3]} id={row[5]} "
                f"runtime_kinds={row[8]} session_source={row[9]}"
            )
    console.print(f"[dim]Workspace root:[/dim] {workspace_root.as_posix()}")


@hooks_app.command("doctor")
def hooks_doctor(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    statuses = _collect_hook_source_statuses(workspace_root=workspace_root)
    table = _Table(title="hooks doctor")
    table.add_column("source")
    table.add_column("status")
    table.add_column("trust")
    table.add_column("events", justify="right")
    table.add_column("hooks", justify="right")
    table.add_column("path")
    for status in statuses:
        table.add_row(
            status.source_scope,
            status.status,
            "trusted" if status.trusted else "untrusted",
            str(status.event_count),
            str(status.hook_count),
            status.path.as_posix(),
        )
    console.print(table)
    for status in statuses:
        console.print(f"[dim]{status.source_scope} path:[/dim] {status.path.as_posix()}")
    for status in statuses:
        if status.issue:
            console.print(
                f"[yellow]Hook config issue:[/yellow] {status.path.as_posix()} ({status.issue})"
            )
    try:
        resolved = load_resolved_hooks_config(workspace_root)
        console.print(
            "Effective loaded hooks: "
            + ", ".join(
                f"{event}={len(groups)}" for event, groups in resolved.groups_by_event.items()
            )
            if resolved.groups_by_event
            else "Effective loaded hooks: none"
        )
        for event_name, groups in resolved.groups_by_event.items():
            for group in groups:
                if not group.matcher:
                    continue
                try:
                    re.compile(group.matcher)
                except re.error as exc:
                    console.print(
                        f"[red]Matcher error:[/red] {event_name} in {group.source_path.as_posix()}: "
                        f"invalid regex {group.matcher!r} ({exc})"
                    )
        for untrusted in resolved.untrusted_project_paths:
            console.print(
                f"[yellow]Untrusted project hooks config (ignored):[/yellow] "
                f"{untrusted.as_posix()}. Run "
                f"`alysis hooks trust --path {workspace_root.as_posix()}` to allow it."
            )
    except ConfigError as exc:
        console.print(f"[yellow]Effective load failed:[/yellow] {exc}")


@hooks_app.command("trace")
def hooks_trace(
    session_id: str | None = typer.Argument(
        None,
        help="Session id (filename stem). If omitted, reads the latest retained session.",
    ),
    limit: int = typer.Option(200, "--limit", min=1, help="Maximum events to display."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    sessions_dir = resolve_sessions_dir(cfg)
    resolved_session_id = session_id
    if not resolved_session_id:
        all_infos = _patchable("list_sessions", list_sessions)(sessions_dir)
        infos = filter_sessions_to_local_owner(all_infos)
        if not infos:
            if all_infos:
                console.print(
                    f"No sessions owned by this account in {sessions_dir} "
                    f"({len(all_infos)} recorded by a different account; "
                    "pass an explicit session id)."
                )
            else:
                console.print(f"No retained sessions found in {sessions_dir}")
            return
        resolved_session_id = infos[0].session_id
    artifact_path = hook_audit_artifact_path(
        sessions_dir=sessions_dir,
        session_id=resolved_session_id,
    )
    events = list(read_hook_audit_events(artifact_path))
    if not events:
        console.print(f"No hook audit log found: {artifact_path}")
        return
    if json_output:
        console.print_json(json.dumps(events))
        return
    table = _Table(title=f"Hook Trace ({resolved_session_id})")
    table.add_column("ts")
    table.add_column("event")
    table.add_column("hook_id")
    table.add_column("status")
    table.add_column("trust")
    table.add_column("scope")
    table.add_column("duration_ms", justify="right")
    table.add_column("warnings")
    for event in events[-limit:]:
        payload = event.get("payload")
        event_payload = payload if isinstance(payload, dict) else event
        warning_previews = event_payload.get("warning_previews") or []
        warning_text = (
            "; ".join(str(item) for item in warning_previews[:2]) if warning_previews else "-"
        )
        table.add_row(
            str(event.get("ts") or "-"),
            str(event_payload.get("event_name") or "-"),
            str(event_payload.get("hook_id") or "-"),
            str(event_payload.get("status") or "-"),
            "trusted" if bool(event_payload.get("trusted")) else "untrusted",
            str(event_payload.get("source_scope") or "-"),
            str(int(event_payload.get("duration_ms") or 0)),
            warning_text,
        )
    console.print(table)
    console.print(f"[dim]Artifact:[/dim] {artifact_path.as_posix()}")


@hooks_app.command("test")
def hooks_test(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
    event: str = typer.Option("SessionStart", "--event", help="Hook event name."),
    tool: str = typer.Option("", "--tool", help="Tool name for Pre/PostToolUse dry-runs."),
    runtime_kind: str = typer.Option(
        RuntimeKind.INTERACTIVE_CHAT.value,
        "--runtime-kind",
        help="Runtime kind for hook matching.",
    ),
    session_source: str = typer.Option(
        "startup",
        "--session-source",
        help="Session source for SessionStart hook matching.",
    ),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    try:
        canonical_event = canonicalize_hook_event_name(event)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    try:
        resolved_runtime_kind = normalize_runtime_kind(runtime_kind).value
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    normalized_session_source = str(session_source or "").strip().lower()
    if normalized_session_source not in _HOOK_SESSION_SOURCES:
        console.print("[red]session_source must be one of: startup, resume, fork[/red]")
        raise typer.Exit(code=2)

    resolved = load_resolved_hooks_config(workspace_root)
    groups = resolved.groups_for_event(canonical_event)
    if not groups:
        console.print(f"No active hooks for event {canonical_event}.")
        return

    table = _Table(title=f"Hooks Test ({canonical_event})")
    detail_rows: list[tuple[str, str, str, str]] = []
    table.add_column("scope")
    table.add_column("trust")
    table.add_column("matcher")
    table.add_column("id")
    table.add_column("runtime_kinds")
    table.add_column("session_source")
    table.add_column("match")
    table.add_column("reason")
    table.add_column("command")
    for group in groups:
        matcher = str(group.matcher or "")
        for hook in group.hooks:
            matched, reason = _hook_test_match_result(
                event_name=canonical_event,
                matcher_target=str(tool or "").strip(),
                runtime_kind=resolved_runtime_kind,
                session_source=normalized_session_source,
                matcher=matcher,
                hook=hook,
            )
            table.add_row(
                group.source_scope,
                "trusted" if group.trusted else "untrusted",
                matcher or "*",
                hook.id or "-",
                ",".join(hook.runtime_kinds or ()) or "-",
                ",".join(hook.session_source or ()) or "-",
                "yes" if matched else "no",
                reason,
                hook.command,
            )
            detail_rows.append((hook.id or "-", "yes" if matched else "no", reason, hook.command))
    console.print(table)
    for hook_id, matched, reason, command in detail_rows:
        if hook_id != "-":
            console.print(
                f"[dim]hook_test:[/dim] id={hook_id} match={matched} "
                f"reason={reason} command={command}"
            )
    if resolved.untrusted_project_paths:
        console.print(
            "[yellow]Ignored untrusted project hook configs:[/yellow] "
            + ", ".join(path.as_posix() for path in resolved.untrusted_project_paths)
        )


@hooks_app.command("trust")
def hooks_trust(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    workspace_root, config_path = _project_hooks_config_or_exit(path=path, console=console)
    trust_project_hooks_config(workspace_root=workspace_root, config_path=config_path)
    console.print(f"Trusted project hooks config: {config_path}")


@hooks_app.command("untrust")
def hooks_untrust(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    workspace_root, config_path = _project_hooks_config_or_exit(
        path=path,
        console=console,
        validate=False,
    )
    untrust_project_hooks_config(workspace_root=workspace_root, config_path=config_path)
    console.print(f"Untrusted project hooks config: {config_path}")


_HOOKS_INIT_TEMPLATE: dict[str, Any] = {
    "schema_version": 1,
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "id": "starter.session-start-log",
                        "description": "Log a readable line when the session begins.",
                        "command": 'echo "[hooks] session started" >> .alysis/demo-hooks.log',
                        "enabled": False,
                    }
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "shell_run",
                "hooks": [
                    {
                        "type": "command",
                        "id": "starter.block-dangerous-shell",
                        "description": "Block obviously dangerous shell commands.",
                        "command": "python3 docs/examples/hooks/block_dangerous.py",
                        "enabled": False,
                    }
                ],
            }
        ],
    },
}


@hooks_app.command("init")
def hooks_init(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing .alysis/hooks.local.json file."
    ),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    local_config_path = project_local_hooks_config_path(workspace_root)
    if local_config_path.exists() and not force:
        console.print(
            f"[yellow]Hooks config already exists:[/yellow] {local_config_path.as_posix()}"
        )
        console.print("Use --force to overwrite.")
        raise typer.Exit(code=1)
    local_config_path.parent.mkdir(parents=True, exist_ok=True)
    local_config_path.write_text(
        json.dumps(_HOOKS_INIT_TEMPLATE, indent=2) + "\n",
        encoding="utf-8",
    )
    console.print(f"Wrote starter hooks config: {local_config_path.as_posix()}")
    gitignore_path = workspace_root / ".gitignore"
    gitignore_entry = ".alysis/hooks.local.json"
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
        already_ignored = any(
            line.strip() == gitignore_entry or line.strip() == ".alysis/"
            for line in existing.splitlines()
        )
        if not already_ignored:
            with gitignore_path.open("a", encoding="utf-8") as handle:
                if not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(f"{gitignore_entry}\n")
            console.print(f"Appended .gitignore entry: {gitignore_entry}")
    console.print("[dim]Local config is trusted by location; no `alysis hooks trust` needed.[/dim]")


@hooks_app.command("effective")
def hooks_effective(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
    event: str = typer.Option(..., "--event", help="Hook event name to resolve."),
    tool: str | None = typer.Option(
        None,
        "--tool",
        help="Tool name for matcher resolution (PreToolUse/PostToolUse/SubagentStop).",
    ),
    runtime: str = typer.Option("interactive_chat", "--runtime", help="Runtime kind to evaluate."),
    session_source: str | None = typer.Option(
        None, "--session-source", help="Session source (startup / resume / fork)."
    ),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    try:
        canonical_event = canonicalize_hook_event_name(event)
    except ValueError as exc:
        console.print(f"[red]Invalid event:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    resolved = load_resolved_hooks_config(workspace_root)
    groups = resolved.groups_for_event(canonical_event)
    table = _Table(title=f"Effective hooks for {canonical_event}")
    table.add_column("order", justify="right")
    table.add_column("hook_id")
    table.add_column("fires")
    table.add_column("reason")
    table.add_column("priority", justify="right")
    table.add_column("source")
    table.add_column("command")
    order = 0
    for group in groups:
        matcher_pass = True
        matcher_reason = ""
        if tool is not None and canonical_event in {"PreToolUse", "PostToolUse", "SubagentStop"}:
            if group.matcher:
                try:
                    matcher_pass = re.compile(group.matcher).search(tool) is not None
                    if not matcher_pass:
                        matcher_reason = f"matcher {group.matcher!r} does not match {tool!r}"
                except re.error as exc:
                    matcher_pass = False
                    matcher_reason = f"matcher regex error: {exc}"
        for hook in group.hooks:
            order += 1
            fires = matcher_pass
            reason = matcher_reason
            if fires and hook.runtime_kinds and runtime not in set(hook.runtime_kinds):
                fires = False
                reason = f"runtime_kind {runtime!r} not in {list(hook.runtime_kinds)}"
            if fires and hook.session_source:
                if session_source is None:
                    fires = False
                    reason = "sessionSource filter set but --session-source not provided"
                elif session_source not in set(hook.session_source):
                    fires = False
                    reason = f"session_source {session_source!r} not in {list(hook.session_source)}"
            table.add_row(
                str(order),
                hook.id or "-",
                "yes" if fires else "no",
                reason or "-",
                str(hook.priority),
                f"{group.source_scope}:{group.source_path.name}",
                hook.command,
            )
    if order == 0:
        console.print(f"No effective hooks for {canonical_event}.")
        return
    console.print(table)


def _find_hook_in_layer(
    *,
    path: Path,
    hook_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    hooks_root = raw.get("hooks")
    if not isinstance(hooks_root, dict):
        return None
    for groups in hooks_root.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            hook_list = group.get("hooks")
            if not isinstance(hook_list, list):
                continue
            for entry in hook_list:
                if isinstance(entry, dict) and str(entry.get("id") or "") == hook_id:
                    return raw, entry
    return None


def _hooks_config_path_for_layer(
    *,
    workspace_root: Path,
    layer: str,
) -> Path:
    if layer == "user":
        return user_hooks_config_path()
    if layer == "project":
        return project_hooks_config_path(workspace_root)
    if layer == "local":
        return project_local_hooks_config_path(workspace_root)
    raise typer.BadParameter("layer must be one of: user, project, local.")


def _set_hook_enabled(
    *,
    path: Path,
    hook_id: str,
    enabled: bool,
    console: Any,
) -> None:
    if not path.exists():
        console.print(f"[red]Config not found:[/red] {path.as_posix()}")
        raise typer.Exit(code=1)
    result = _find_hook_in_layer(path=path, hook_id=hook_id)
    if result is None:
        console.print(f"[red]Hook id not found in layer:[/red] {hook_id!r} at {path.as_posix()}")
        raise typer.Exit(code=1)
    raw, entry = result
    entry["enabled"] = enabled
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    verb = "Enabled" if enabled else "Disabled"
    console.print(f"{verb} hook {hook_id!r} in {path.as_posix()}")


@hooks_app.command("enable")
def hooks_enable(
    hook_id: str = typer.Argument(..., help="Hook id to enable."),
    layer: str = typer.Option("local", "--layer", help="Config layer: user, project, or local."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    config_path = _hooks_config_path_for_layer(workspace_root=workspace_root, layer=layer)
    _set_hook_enabled(path=config_path, hook_id=hook_id, enabled=True, console=console)


@hooks_app.command("disable")
def hooks_disable(
    hook_id: str = typer.Argument(..., help="Hook id to disable."),
    layer: str = typer.Option("local", "--layer", help="Config layer: user, project, or local."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    config_path = _hooks_config_path_for_layer(workspace_root=workspace_root, layer=layer)
    _set_hook_enabled(path=config_path, hook_id=hook_id, enabled=False, console=console)


@hooks_app.command("watch")
def hooks_watch(
    session_id: str | None = typer.Argument(
        None,
        help="Session id. Defaults to the latest retained session.",
    ),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum events to show."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    sessions_dir = resolve_sessions_dir(cfg)
    resolved_session_id = session_id
    if not resolved_session_id:
        all_infos = _patchable("list_sessions", list_sessions)(sessions_dir)
        infos = filter_sessions_to_local_owner(all_infos)
        if not infos:
            if all_infos:
                console.print(
                    f"No sessions owned by this account in {sessions_dir} "
                    f"({len(all_infos)} recorded by a different account; "
                    "pass an explicit session id)."
                )
            else:
                console.print(f"No retained sessions found in {sessions_dir}")
            return
        resolved_session_id = infos[0].session_id
    artifact_path = hook_audit_artifact_path(
        sessions_dir=sessions_dir,
        session_id=resolved_session_id,
    )
    events = list(read_hook_audit_events(artifact_path))
    if not events:
        console.print(f"No hook audit events yet at {artifact_path}")
        return
    status_color = {
        "ok": "green",
        "warning": "yellow",
        "blocked": "red",
    }
    for event in events[-limit:]:
        status = str(event.get("status") or "-")
        color = status_color.get(status, STYLE_CONTENT or "default")
        event_name = str(event.get("event_name") or "-")
        hook_id = str(event.get("hook_id") or "-")
        duration_ms = int(event.get("duration_ms") or 0)
        ts = str(event.get("ts") or "-")
        console.print(
            f"[dim]{ts}[/dim] [{color}]{status:<8}[/] {event_name:<20} {hook_id:<30} "
            f"[dim]{duration_ms}ms[/dim]"
        )
    console.print(f"[dim]artifact:[/dim] {artifact_path.as_posix()}")
