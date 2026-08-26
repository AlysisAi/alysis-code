from __future__ import annotations

from pathlib import Path

import typer

from ...skills import load_repo_conventions, render_repo_conventions_context
from ._shared import _console, _resolve_tool_workspace_root, _Table

conventions_app = typer.Typer(
    add_completion=False,
    help="Repo conventions diagnostics for AGENTS.md / CLAUDE.md / CONVENTIONS.md.",
)


@conventions_app.command("list")
def conventions_list(
    path: Path = typer.Option(Path("."), "--path", help="Workspace, repository, or file path."),
) -> None:
    console = _console()
    focus_path = path.expanduser().resolve()
    workspace_root = _resolve_tool_workspace_root(
        path=focus_path if focus_path.is_dir() else focus_path.parent
    )
    documents = load_repo_conventions(
        focus_path=focus_path,
        workspace_root=workspace_root,
    )
    console.print(f"[dim]Workspace root:[/dim] {workspace_root.as_posix()}")
    console.print(f"[dim]Focus path:[/dim] {focus_path.as_posix()}")
    if not documents:
        console.print("No repo conventions found.")
        return

    table = _Table(title="Repo Conventions")
    table.add_column("priority")
    table.add_column("name")
    table.add_column("trust")
    table.add_column("chars")
    table.add_column("path")
    for priority, document in enumerate(documents, start=1):
        try:
            display_path = document.path.relative_to(workspace_root).as_posix()
        except ValueError:
            display_path = document.path.as_posix()
        table.add_row(
            str(priority),
            document.name,
            document.trust_level,
            str(len(document.content)),
            display_path,
        )
    console.print(table)
    console.print("[dim]Priority order is highest precedence first within repo conventions.[/dim]")


@conventions_app.command("render")
def conventions_render(
    path: Path = typer.Option(Path("."), "--path", help="Workspace, repository, or file path."),
    max_chars: int = typer.Option(
        4000,
        "--max-chars",
        min=256,
        help="Maximum rendered repo-conventions context size.",
    ),
) -> None:
    console = _console()
    focus_path = path.expanduser().resolve()
    workspace_root = _resolve_tool_workspace_root(
        path=focus_path if focus_path.is_dir() else focus_path.parent
    )
    documents = load_repo_conventions(
        focus_path=focus_path,
        workspace_root=workspace_root,
    )
    rendered = render_repo_conventions_context(
        documents=documents,
        max_chars=max_chars,
    )
    console.print(f"[dim]Workspace root:[/dim] {workspace_root.as_posix()}")
    console.print(f"[dim]Focus path:[/dim] {focus_path.as_posix()}")
    if rendered is None:
        console.print("No repo conventions found.")
        return
    console.print(rendered, end="")
