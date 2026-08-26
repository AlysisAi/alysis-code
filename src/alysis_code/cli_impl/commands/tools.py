from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ...config import AppConfig, load_config
from ...custom_tools import (
    CustomToolCatalogEntry,
    build_custom_tool_session_state,
    global_custom_tools_root,
    project_custom_tools_root,
    trust_project_tool,
    untrust_project_tool,
)
from ...runtime_kind import RuntimeKind
from ...surface.styles import STYLE_CONTENT, STYLE_EMPHASIS
from ...tools.registry import iter_builtin_tool_metadata
from . import _patchable
from ._shared import _console, _resolve_tool_workspace_root, _Table

if TYPE_CHECKING:
    from rich.table import Table


tool_app = typer.Typer(add_completion=False, help="Custom tool discovery and trust commands.")


def _discover_custom_tools_for_path(
    *,
    path: Path,
    cfg: AppConfig | None = None,
) -> tuple[Path, Any]:
    workspace_root = _resolve_tool_workspace_root(path=path)
    effective_cfg = cfg or _patchable("load_config", load_config)()
    state = build_custom_tool_session_state(
        workspace_root=workspace_root,
        custom_tools_enabled=bool(getattr(effective_cfg, "custom_tools_enabled", True)),
        mode="review",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        built_in_tool_names={spec.name.casefold() for spec in iter_builtin_tool_metadata()},
        catalog_view=True,
    )
    return workspace_root, state


def _resolve_custom_tool_entries_by_name(
    *,
    state: Any,
    name: str,
) -> list[CustomToolCatalogEntry]:
    needle = str(name or "").strip().casefold()
    if not needle:
        return []
    return [
        entry
        for entry in getattr(state, "catalog_entries", ())
        if str(getattr(entry, "name", "")).casefold() == needle
    ]


_CUSTOM_TOOLS_WIDE_TABLE_MIN_WIDTH = 140


def _custom_tool_source_filename(entry: CustomToolCatalogEntry) -> str:
    return entry.source_path.name or entry.source_path.as_posix()


def _custom_tool_source_location(*, entry: CustomToolCatalogEntry, workspace_root: Path) -> str:
    relative_path = ""
    if entry.spec is not None and entry.spec.relative_tool_path:
        relative_path = entry.spec.relative_tool_path
    elif entry.issue is not None and entry.issue.relative_tool_path:
        relative_path = entry.issue.relative_tool_path
    elif entry.source_scope == "project":
        try:
            relative_path = (
                entry.source_path.resolve().relative_to(workspace_root.resolve()).as_posix()
            )
        except ValueError:
            relative_path = ""
    else:
        try:
            relative_path = (
                entry.source_path.resolve()
                .relative_to(global_custom_tools_root().resolve())
                .as_posix()
            )
        except ValueError:
            relative_path = ""
    relative_path = str(relative_path or "").strip().replace("\\", "/")
    if entry.source_scope == "global" and relative_path:
        global_prefix = global_custom_tools_root().name.strip().replace("\\", "/")
        if (
            global_prefix
            and relative_path != global_prefix
            and not relative_path.startswith(f"{global_prefix}/")
        ):
            return f"{global_prefix}/{relative_path}"
    if relative_path:
        return relative_path
    if entry.source_scope == "global":
        global_prefix = global_custom_tools_root().name.strip().replace("\\", "/")
        if global_prefix:
            return f"{global_prefix}/{_custom_tool_source_filename(entry)}"
        return _custom_tool_source_filename(entry)
    return _custom_tool_source_filename(entry)


def _custom_tools_table(
    *,
    entries: list[CustomToolCatalogEntry],
    title: str,
    workspace_root: Path,
) -> Table:
    table = _Table(title=title, expand=True)
    table.add_column("name", no_wrap=True, ratio=2)
    table.add_column("scope", no_wrap=True)
    table.add_column("trust", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("file", no_wrap=True)
    table.add_column("location", overflow="fold", ratio=4)
    table.add_column("details", overflow="fold", ratio=3)
    for entry in entries:
        table.add_row(
            entry.name,
            entry.source_scope,
            entry.trust,
            entry.status,
            _custom_tool_source_filename(entry),
            _custom_tool_source_location(entry=entry, workspace_root=workspace_root),
            entry.detail or "-",
        )
    return table


def _custom_tools_stacked_list(
    *,
    entries: list[CustomToolCatalogEntry],
    title: str,
    workspace_root: Path,
) -> Any:
    from rich.console import Group
    from rich.text import Text

    renderables: list[Any] = [Text(str(title), style=STYLE_EMPHASIS)]
    for index, entry in enumerate(entries):
        if index:
            renderables.append(Text(""))
        renderables.append(
            Text.assemble(
                (entry.name, STYLE_EMPHASIS),
                (" · ", "bright_black"),
                (entry.source_scope, "cyan"),
                (" · ", "bright_black"),
                (entry.trust, STYLE_CONTENT),
                (" · ", "bright_black"),
                (entry.status, STYLE_CONTENT),
            )
        )
        renderables.append(
            Text.assemble(
                ("  file: ", "dim"),
                (_custom_tool_source_filename(entry), STYLE_CONTENT),
            )
        )
        renderables.append(
            Text.assemble(
                ("  source: ", "dim"),
                (
                    _custom_tool_source_location(entry=entry, workspace_root=workspace_root),
                    STYLE_CONTENT,
                ),
            )
        )
        if entry.detail:
            renderables.append(Text.assemble(("  details: ", "dim"), (entry.detail, STYLE_CONTENT)))
    return Group(*renderables)


def _custom_tools_list_renderable(
    *,
    entries: list[CustomToolCatalogEntry],
    title: str,
    workspace_root: Path,
    width: int,
) -> Any:
    if width >= _CUSTOM_TOOLS_WIDE_TABLE_MIN_WIDTH:
        return _custom_tools_table(entries=entries, title=title, workspace_root=workspace_root)
    return _custom_tools_stacked_list(entries=entries, title=title, workspace_root=workspace_root)


@tool_app.command("list")
def tool_list(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    workspace_root, state = _discover_custom_tools_for_path(path=path, cfg=cfg)
    entries = list(state.catalog_entries)
    if not entries:
        console.print("No custom tools discovered.")
        console.print(
            f"[dim]Project root:[/dim] {project_custom_tools_root(workspace_root).as_posix()}"
        )
        console.print(f"[dim]Global root:[/dim] {global_custom_tools_root().as_posix()}")
        return
    console.print(
        _custom_tools_list_renderable(
            entries=entries,
            title=f"Custom Tools ({len(entries)})",
            workspace_root=workspace_root,
            width=console.width,
        )
    )
    console.print(
        f"[dim]Project root:[/dim] {project_custom_tools_root(workspace_root).as_posix()}"
    )
    console.print(f"[dim]Global root:[/dim] {global_custom_tools_root().as_posix()}")


@tool_app.command("info")
def tool_info(
    name: str = typer.Argument(..., help="Custom tool name."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    workspace_root, state = _discover_custom_tools_for_path(path=path, cfg=cfg)
    matches = _resolve_custom_tool_entries_by_name(state=state, name=name)
    if not matches:
        console.print(f"[red]Custom tool not found:[/red] {name}")
        raise typer.Exit(code=1)
    for entry in matches:
        table = _Table(title=f"Custom Tool: {entry.name}")
        table.add_column("field")
        table.add_column("value", overflow="fold")
        table.add_row("scope", entry.source_scope)
        table.add_row("trust", entry.trust)
        table.add_row("status", entry.status)
        table.add_row("source_path", entry.source_path.as_posix())
        table.add_row("workspace_root", workspace_root.as_posix())
        if entry.detail:
            table.add_row("details", entry.detail)
        if entry.spec is not None:
            table.add_row("description", entry.spec.description)
            table.add_row("manifest_version", str(entry.spec.manifest_version))
            table.add_row("timeout_s", f"{entry.spec.timeout_s:g}")
            table.add_row("isolation", entry.spec.isolation)
            table.add_row(
                "capabilities",
                json.dumps(
                    entry.spec.capabilities.to_dict(),
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
            table.add_row("enabled_in", ", ".join(entry.spec.enabled_in))
            table.add_row(
                "required_env",
                ", ".join(entry.spec.required_env) if entry.spec.required_env else "-",
            )
            table.add_row(
                "missing_env",
                ", ".join(entry.spec.missing_env) if entry.spec.missing_env else "-",
            )
            table.add_row(
                "input_schema",
                json.dumps(entry.spec.input_schema, ensure_ascii=True, sort_keys=True),
            )
            table.add_row(
                "output_schema",
                (
                    json.dumps(entry.spec.output_schema, ensure_ascii=True, sort_keys=True)
                    if entry.spec.output_schema is not None
                    else "-"
                ),
            )
        console.print(table)
        console.print(f"[dim]source_path:[/dim] {entry.source_path.as_posix()}")
        console.print(f"[dim]workspace_root:[/dim] {workspace_root.as_posix()}")


def _select_project_tool_entry_or_exit(
    *,
    state: Any,
    name: str,
    console: Any,
) -> CustomToolCatalogEntry:
    matches = _resolve_custom_tool_entries_by_name(state=state, name=name)
    if not matches:
        console.print(f"[red]Custom tool not found:[/red] {name}")
        raise typer.Exit(code=1)
    for entry in matches:
        if (
            entry.source_scope == "project"
            and entry.spec is not None
            and entry.status != "shadowed"
        ):
            return entry
    if any(entry.source_scope == "global" for entry in matches):
        console.print("[red]Trust commands only apply to project custom tools.[/red]")
        raise typer.Exit(code=2)
    console.print("[red]No valid project custom tool matched that name.[/red]")
    raise typer.Exit(code=2)


@tool_app.command("trust")
def tool_trust(
    name: str = typer.Argument(..., help="Project custom tool name."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    _workspace_root, state = _discover_custom_tools_for_path(path=path, cfg=cfg)
    entry = _select_project_tool_entry_or_exit(state=state, name=name, console=console)
    assert entry.spec is not None
    trust_project_tool(entry.spec)
    console.print(f"Trusted project custom tool: {entry.spec.name}")


@tool_app.command("untrust")
def tool_untrust(
    name: str = typer.Argument(..., help="Project custom tool name."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    cfg = _patchable("load_config", load_config)()
    _workspace_root, state = _discover_custom_tools_for_path(path=path, cfg=cfg)
    entry = _select_project_tool_entry_or_exit(state=state, name=name, console=console)
    assert entry.spec is not None
    untrust_project_tool(entry.spec)
    console.print(f"Untrusted project custom tool: {entry.spec.name}")
