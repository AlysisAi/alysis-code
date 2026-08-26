from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ...skills import (
    discover_skills,
    install_skill_bundle,
    load_global_skill_state,
    load_project_skill_state,
    remove_managed_skill,
    render_skill_info_text,
    resolve_skill_catalog,
    resolve_skill_catalog_entry,
    save_global_skill_state,
    save_project_skill_state,
    scaffold_skill_bundle,
    set_global_skill_disabled,
    set_project_skill_override,
    validate_skill_bundle,
)
from ...workspace_context import resolve_workspace_context
from . import _patchable
from ._shared import _console, _resolve_tool_workspace_root, _Table

if TYPE_CHECKING:
    from rich.console import Console
    from rich.table import Table


skill_app = typer.Typer(
    add_completion=False,
    help="Skill discovery, authoring, validation, and lifecycle commands.",
)


def _discover_skills_for_path(
    *,
    path: Path,
) -> Any:
    resolved = path.resolve()
    workspace_context = _patchable("resolve_workspace_context", resolve_workspace_context)(
        resolved if resolved.is_dir() else resolved.parent
    )
    return discover_skills(
        focus_path=workspace_context.focus_path,
        workspace_root=workspace_context.workspace_root,
    )


def _resolve_skill_catalog_for_path(
    *,
    path: Path,
) -> tuple[Path, Any]:
    resolved = path.resolve()
    workspace_context = _patchable("resolve_workspace_context", resolve_workspace_context)(
        resolved if resolved.is_dir() else resolved.parent
    )
    discovered = discover_skills(
        focus_path=workspace_context.focus_path,
        workspace_root=workspace_context.workspace_root,
    )
    catalog = resolve_skill_catalog(
        discovered=discovered,
        workspace_root=workspace_context.workspace_root,
    )
    return workspace_context.workspace_root, catalog


def _print_skill_catalog_issues(*, console: Console, catalog: Any) -> None:
    for issue in getattr(catalog.effective, "issues", ()):
        console.print(f"[yellow]Skipped skill:[/yellow] {issue.source_path} ({issue.message})")
    for issue in getattr(catalog, "lifecycle_issues", ()):
        console.print(
            f"[yellow]Lifecycle state warning:[/yellow] {issue.source_path} ({issue.message})"
        )


def _skills_table(*, skills: list[Any] | tuple[Any, ...], title: str) -> Table:
    table = _Table(title=title)
    table.add_column("name", no_wrap=True)
    table.add_column("enabled", no_wrap=True)
    table.add_column("managed", no_wrap=True)
    table.add_column("description")
    table.add_column("source", no_wrap=True)
    table.add_column("trust", no_wrap=True)
    table.add_column("path")
    for skill in skills:
        enabled = getattr(skill, "enabled", None)
        managed = getattr(skill, "managed", None)
        bundle = getattr(skill, "skill", skill)
        table.add_row(
            str(getattr(bundle, "name", "")),
            "-" if enabled is None else ("yes" if enabled else "no"),
            "-" if managed is None else ("yes" if managed else "no"),
            str(getattr(bundle, "description", "")),
            (
                f"{getattr(bundle, 'source_scope', '')}/"
                f"{getattr(bundle, 'source_kind', '')}/"
                f"{getattr(bundle, 'source_family', '')}"
            ),
            str(getattr(bundle, "trust_level", "")),
            str(getattr(bundle, "source_path", "")),
        )
    return table


def _render_skill_validation_summary(result: Any) -> str:
    lines = [
        f"bundle_path: {getattr(result, 'bundle_path', '')}",
        f"valid: {'yes' if getattr(result, 'valid', False) else 'no'}",
    ]
    name = str(getattr(result, "name", "") or "").strip()
    if name:
        lines.append(f"name: {name}")
    description = str(getattr(result, "description", "") or "").strip()
    if description:
        lines.append(f"description: {description}")
    issues = tuple(getattr(result, "issues", ()) or ())
    if issues:
        lines.append("issues:")
        for issue in issues:
            severity = str(getattr(issue, "severity", "error") or "error")
            message = str(getattr(issue, "message", "") or "").strip()
            issue_path = str(getattr(issue, "path", "") or "").strip()
            lines.append(f"- [{severity}] {message} ({issue_path})")
    return "\n".join(lines)


@skill_app.command("list")
def skill_list(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    _workspace_root, catalog = _resolve_skill_catalog_for_path(path=path)
    if not catalog.entries:
        console.print("No skills discovered in the supported skill roots.")
        _print_skill_catalog_issues(console=console, catalog=catalog)
        return
    console.print(_skills_table(skills=catalog.entries, title=f"Skills ({len(catalog.entries)})"))
    _print_skill_catalog_issues(console=console, catalog=catalog)


@skill_app.command("info")
def skill_info(
    name: str = typer.Argument(..., help="Skill name or alias."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    _workspace_root, catalog = _resolve_skill_catalog_for_path(path=path)
    entry = resolve_skill_catalog_entry(entries=catalog.entries, raw_name=name)
    if entry is None:
        _print_skill_catalog_issues(console=console, catalog=catalog)
        console.print(f"[red]Skill not found:[/red] {name}")
        if catalog.entries:
            console.print(
                "Available skills: "
                + ", ".join(str(getattr(item.skill, "name", "")) for item in catalog.entries)
            )
        raise typer.Exit(code=1)
    console.print(render_skill_info_text(entry.skill, catalog_entry=entry))
    _print_skill_catalog_issues(console=console, catalog=catalog)


@skill_app.command("init")
@skill_app.command("create")
def skill_init(
    name: str = typer.Argument(..., help="Skill display name."),
    description: str = typer.Option("", "--description", help="Short skill description."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace path for project skills."),
    project: bool = typer.Option(False, "--project", help="Create the skill in the project root."),
    user: bool = typer.Option(
        False, "--user", help="Create the skill in the user-global managed root."
    ),
    family: str = typer.Option(
        "native", "--family", help="Project family: native|agents|claude|github."
    ),
    portable: bool = typer.Option(
        False, "--portable", help="Project-local portable alias for --family agents."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing scaffold."),
) -> None:
    console = _console()
    if project and user:
        console.print("[red]Choose only one of --project or --user.[/red]")
        raise typer.Exit(code=2)
    use_project = not user
    selected_family = "agents" if portable else family
    if not use_project and portable:
        console.print("[red]--portable can only be used with project-local scaffolds.[/red]")
        raise typer.Exit(code=2)
    if not use_project and str(family or "native").strip().lower() != "native":
        console.print("[red]--family only applies to project-local scaffolds.[/red]")
        raise typer.Exit(code=2)
    workspace_root = _resolve_tool_workspace_root(path=path)
    try:
        result = scaffold_skill_bundle(
            name=name,
            description=description,
            workspace_root=workspace_root,
            project=use_project,
            family=selected_family,
            force=force,
        )
    except Exception as exc:
        console.print(f"[red]Skill scaffold failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Created skill scaffold: {result.bundle_path}")
    console.print("Next steps:")
    console.print(f"1. Edit {result.bundle_path / 'SKILL.md'}")
    console.print(f"2. Run `alysis skill validate {result.bundle_path}`")
    if result.managed:
        console.print(
            "3. The skill is already in a managed native root and can be enabled/disabled later."
        )
    else:
        console.print(
            "3. This portable scaffold is unmanaged; lifecycle remove/install commands will not manage it automatically."
        )


@skill_app.command("validate")
def skill_validate(
    bundle: Path | None = typer.Argument(None, help="Path to a skill bundle directory."),
    name: str | None = typer.Option(None, "--name", help="Validate a discovered skill by name."),
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path for discovery or project state."
    ),
    validate_all: bool = typer.Option(
        False, "--all", help="Validate all discovered skills for the workspace."
    ),
) -> None:
    console = _console()
    if sum(1 for item in (bundle is not None, bool(name), validate_all) if item) != 1:
        console.print("[red]Choose exactly one of <bundle>, --name, or --all.[/red]")
        raise typer.Exit(code=2)
    results: list[Any] = []
    if bundle is not None:
        results.append(validate_skill_bundle(bundle))
    else:
        _workspace_root, catalog = _resolve_skill_catalog_for_path(path=path)
        if validate_all:
            seen_paths: set[Path] = set()
            for entry in catalog.entries:
                if entry.skill.bundle_path in seen_paths:
                    continue
                seen_paths.add(entry.skill.bundle_path)
                results.append(validate_skill_bundle(entry.skill.bundle_path))
            for issue in catalog.effective.issues:
                message = str(getattr(issue, "message", "") or "")
                if message.startswith("failed to scan skill root"):
                    continue
                source_path = getattr(issue, "source_path", None)
                if (
                    isinstance(source_path, Path)
                    and source_path.is_dir()
                    and source_path not in seen_paths
                ):
                    seen_paths.add(source_path)
                    results.append(validate_skill_bundle(source_path))
        else:
            entry = resolve_skill_catalog_entry(entries=catalog.entries, raw_name=name or "")
            if entry is None:
                console.print(f"[red]Skill not found:[/red] {name}")
                raise typer.Exit(code=1)
            results.append(validate_skill_bundle(entry.skill.bundle_path))
    exit_code = 0
    for result in results:
        console.print(_render_skill_validation_summary(result))
        console.print()
        if not getattr(result, "valid", False):
            exit_code = 1
    if exit_code:
        raise typer.Exit(code=exit_code)


@skill_app.command("install")
def skill_install(
    source: str = typer.Argument(..., help="Local dir, zip archive, or git repository URL."),
    subdir: str | None = typer.Option(
        None, "--subdir", help="Subdirectory containing the skill bundle."
    ),
    project: bool = typer.Option(
        False, "--project", help="Install into the managed project-local native root."
    ),
    path: Path = typer.Option(Path("."), "--path", help="Workspace path for project installs."),
    force: bool = typer.Option(False, "--force", help="Replace an existing managed install."),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    try:
        result = install_skill_bundle(
            source=source,
            workspace_root=workspace_root,
            project=project,
            subdir=subdir,
            force=force,
        )
    except Exception as exc:
        console.print(f"[red]Skill install failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Installed skill: {result.installed_name}")
    console.print(f"bundle_path: {result.bundle_path}")
    console.print(f"source_kind: {result.source_kind}")
    if result.source_commit:
        console.print(f"source_commit: {result.source_commit}")


def _set_skill_enabled_state(
    *,
    name: str,
    project: bool,
    path: Path,
    enabled: bool,
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    try:
        _workspace_root, catalog = _resolve_skill_catalog_for_path(path=path)
        key = str(name or "").strip().casefold()
        if project:
            project_state = load_project_skill_state(workspace_root)
            if (
                resolve_skill_catalog_entry(entries=catalog.entries, raw_name=name) is None
                and key not in project_state.managed_installs
            ):
                raise RuntimeError(f"Skill not found for project override: {name}")
            state = set_project_skill_override(
                project_state,
                name=name,
                enabled=enabled,
            )
            save_project_skill_state(workspace_root, state)
            console.print(
                f"{'Enabled' if enabled else 'Disabled'} skill for project workspace: {name}"
            )
            return
        global_state = load_global_skill_state()
        visible_user_skill = next(
            (
                entry
                for entry in catalog.entries
                if str(getattr(entry.skill, "source_scope", "")) == "user"
                and key in entry.skill.lookup_keys()
            ),
            None,
        )
        if visible_user_skill is None and key not in global_state.managed_installs:
            raise RuntimeError(f"Global skill not found: {name}")
        state = set_global_skill_disabled(
            global_state,
            name=name,
            disabled=not enabled,
        )
        save_global_skill_state(state)
        console.print(f"{'Enabled' if enabled else 'Disabled'} global skill: {name}")
    except Exception as exc:
        console.print(f"[red]Skill state update failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@skill_app.command("enable")
def skill_enable(
    name: str = typer.Argument(..., help="Skill name."),
    project: bool = typer.Option(
        False, "--project", help="Apply the toggle as a project override."
    ),
    path: Path = typer.Option(Path("."), "--path", help="Workspace path for project overrides."),
) -> None:
    _set_skill_enabled_state(name=name, project=project, path=path, enabled=True)


@skill_app.command("disable")
def skill_disable(
    name: str = typer.Argument(..., help="Skill name."),
    project: bool = typer.Option(
        False, "--project", help="Apply the toggle as a project override."
    ),
    path: Path = typer.Option(Path("."), "--path", help="Workspace path for project overrides."),
) -> None:
    _set_skill_enabled_state(name=name, project=project, path=path, enabled=False)


@skill_app.command("remove")
@skill_app.command("uninstall")
def skill_remove(
    name: str = typer.Argument(..., help="Managed skill name."),
    project: bool = typer.Option(
        False, "--project", help="Remove from the managed project-local native root."
    ),
    path: Path = typer.Option(Path("."), "--path", help="Workspace path for project removals."),
) -> None:
    console = _console()
    workspace_root = _resolve_tool_workspace_root(path=path)
    try:
        result = remove_managed_skill(name=name, workspace_root=workspace_root, project=project)
    except Exception as exc:
        console.print(f"[red]Skill remove failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Removed managed skill: {result.removed_name}")
    if result.bundle_path is not None:
        console.print(f"bundle_path: {result.bundle_path}")
