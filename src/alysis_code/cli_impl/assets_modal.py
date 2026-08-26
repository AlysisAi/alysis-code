from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..assets import (
    AssetAlreadyExistsError,
    AssetError,
    AssetSurface,
    AssetSurfaceDetail,
)
from ..assets.ingestion import _classify_asset
from ..assets.surface import build_asset_surface
from ..branding import env_get
from ..config import AppConfig
from ..forge import RunPaths, load_plan
from ..surface.console import make_console

_STATUS_EMOJI = {
    "ready": "🟢",
    "pending": "🟡",
    "running": "🟡",
    "failed": "🔴",
    "minimal": "⚪",
}
_STATUS_TEXT = {
    "ready": "[ready]",
    "pending": "[pending]",
    "running": "[running]",
    "failed": "[failed]",
    "minimal": "[minimal]",
}


def run_assets_modal(
    *,
    cfg: AppConfig,
    run_paths: RunPaths,
    surface: AssetSurface | None = None,
    console: Console | None = None,
) -> None:
    console = console or make_console()
    if surface is None:
        load_plan(run_paths)
        surface = build_asset_surface(cfg=cfg, run_paths=run_paths)
    if not sys.stdin.isatty():
        console.print("Assets modal is unavailable when stdin is not a terminal.")
        return
    while True:
        choice = _prompt_choice(
            console=console,
            title=f"Assets · run {run_paths.run_id}",
            choices=[
                ("add", "Add a new asset"),
                ("list", f"See all assets ({len(surface.list_assets())})"),
                ("back", "Back"),
            ],
        )
        if choice in {None, "back"}:
            return
        if choice == "add":
            _add_asset_flow(console=console, surface=surface, run_paths=run_paths)
        elif choice == "list":
            _list_flow(console=console, surface=surface)


def status_label(status: str, *, pinned: bool = False, emoji: bool | None = None) -> str:
    use_emoji = terminal_supports_emoji() if emoji is None else emoji
    status_key = str(status or "pending").strip().lower()
    if use_emoji:
        prefix = "📌" if pinned else ""
        return f"{prefix}{_STATUS_EMOJI.get(status_key, _STATUS_EMOJI['pending'])}"
    prefix = "[pinned]" if pinned else ""
    return f"{prefix}{_STATUS_TEXT.get(status_key, _STATUS_TEXT['pending'])}"


def terminal_supports_emoji() -> bool:
    if env_get("ALYSIS_NO_EMOJI") in {"1", "true", "yes", "on"}:
        return False
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "🟢📌".encode(encoding)
    except UnicodeEncodeError:
        return False
    return True


def _add_asset_flow(*, console: Console, surface: AssetSurface, run_paths: RunPaths) -> None:
    source = _prompt_path(console=console, root=run_paths.root)
    if source is None:
        return
    title = _prompt_limited_text(console, "Title", required=True, max_chars=80)
    if title is None:
        return
    description = _prompt_limited_text(console, "Description", required=False, max_chars=240)
    if description is None:
        return
    pinned = typer.confirm("Pinned", default=False)
    console.print(
        Panel(
            "\n".join(
                [
                    f"Path: {source}",
                    f"Title: {title}",
                    f"Description: {description or '-'}",
                    f"Pinned: {'yes' if pinned else 'no'}",
                ]
            ),
            title="Confirm Asset",
            border_style="bright_black",
        )
    )
    if not typer.confirm("Add asset", default=True):
        console.print("Cancelled.")
        return
    try:
        result = surface.add_asset(
            source,
            title=title,
            description=description,
            pinned=pinned,
            added_by={"phase": "modal", "command": "/assets"},
            comprehend="async",
            dedupe_policy="reject",
        )
    except AssetAlreadyExistsError as exc:
        _handle_collision(
            console=console, surface=surface, source=source, existing_id=exc.existing_id
        )
        return
    except AssetError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    console.print(f"Asset added: {result.record.id}")
    if result.comprehension_handle is not None:
        console.print("Comprehension started in background.")


def _handle_collision(
    *,
    console: Console,
    surface: AssetSurface,
    source: Path,
    existing_id: str,
) -> None:
    existing = surface.index.get(existing_id, include_deleted=True)
    choice = _prompt_choice(
        console=console,
        title="Duplicate Asset",
        choices=[
            ("use", f'Use existing asset ({existing.id} - "{existing.title}")'),
            ("link", "Return existing asset"),
            ("cancel", "Cancel"),
        ],
    )
    if choice == "use":
        console.print(f"Using existing asset: {existing.id}")
        return
    if choice != "link":
        console.print("Cancelled.")
        return
    result = surface.add_asset(
        source,
        title=existing.title,
        description=existing.description,
        pinned=existing.pinned,
        added_by={"phase": "modal", "command": "/assets"},
        comprehend="async",
        dedupe_policy="link",
    )
    console.print(f"Using existing asset: {result.record.id}")


def _list_flow(*, console: Console, surface: AssetSurface) -> None:
    while True:
        entries = surface.list_assets()
        if not entries:
            console.print("No assets attached.")
            return
        table = Table(title="Assets")
        table.add_column("status")
        table.add_column("kind")
        table.add_column("id")
        table.add_column("title")
        table.add_column("size")
        for entry in entries:
            table.add_row(
                status_label(entry.comprehension_status, pinned=entry.record.pinned),
                "img" if entry.record.kind == "image" else "txt",
                entry.record.id,
                entry.record.title,
                _format_size(entry.record.size_bytes),
            )
        console.print(table)
        choice = _prompt_choice(
            console=console,
            title="Assets",
            choices=[
                *(
                    (entry.record.id, f"{entry.record.id} - {entry.record.title}")
                    for entry in entries
                ),
                ("refresh", "Refresh status"),
                ("back", "Back"),
            ],
        )
        if choice in {None, "back"}:
            return
        if choice == "refresh":
            continue
        _detail_flow(console=console, surface=surface, asset_id=choice)


def _detail_flow(*, console: Console, surface: AssetSurface, asset_id: str) -> None:
    while True:
        try:
            detail = surface.show_asset(asset_id)
        except AssetError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        console.print(_detail_panel(detail))
        deleted = detail.record.deleted_at is not None
        choices = [("back", "Back")]
        if not deleted:
            choices = [
                ("edit", "Edit metadata"),
                ("toggle", "Toggle pinned"),
                ("refresh", "Refresh comprehension"),
                ("text", "View extracted text"),
                ("json", "View comprehension JSON"),
                ("delete", "Delete"),
                ("back", "Back"),
            ]
        choice = _prompt_choice(console=console, title=detail.record.id, choices=choices)
        if choice in {None, "back"}:
            return
        if choice == "edit":
            _edit_metadata(console=console, surface=surface, detail=detail)
        elif choice == "toggle":
            surface.edit_asset(detail.record.id, pinned=not detail.record.pinned)
        elif choice == "refresh":
            surface.refresh_comprehension(detail.record.id, mode="async")
            console.print("Refresh started - status will update on next refresh.")
        elif choice == "text":
            console.print(detail.extracted_text_preview or "No extracted text available")
        elif choice == "json":
            payload = detail.comprehension.to_dict() if detail.comprehension is not None else {}
            console.print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        elif choice == "delete" and typer.confirm(f"Delete {detail.record.id}?", default=False):
            surface.delete_asset(detail.record.id)
            return


def _detail_panel(detail: AssetSurfaceDetail) -> Panel:
    record = detail.record
    comprehension = detail.comprehension
    status = "deleted" if record.deleted_at is not None else detail.comprehension_status
    lines = [
        f"Status: {status}",
        f"Kind: {record.kind} · {_format_size(record.size_bytes)} · {record.mime}",
        f"Original file: {record.original_filename}",
        f"Pinned: {'yes' if record.pinned else 'no'}",
        f"Versions: {', '.join(str(version) for version in detail.versions) or '-'}",
        "",
        "Description:",
        f"  {record.description or '-'}",
    ]
    if record.deleted_at is not None:
        lines.insert(1, f"Deleted at: {record.deleted_at}")
    if comprehension is not None:
        lines.extend(
            [
                "",
                f"Detected lang: {comprehension.detected_language or '-'}",
                f"Source: {comprehension.source}",
                "",
                "Comprehension summary:",
                f"  {comprehension.data.semantic_summary or '-'}",
            ]
        )
        if comprehension.data.stated_facts:
            lines.append("")
            lines.append("Stated facts:")
            lines.extend(f"  - {fact}" for fact in comprehension.data.stated_facts[:8])
    return Panel("\n".join(lines), title=f'{record.id} - "{record.title}"')


def _edit_metadata(*, console: Console, surface: AssetSurface, detail: AssetSurfaceDetail) -> None:
    title = _prompt_limited_text(
        console,
        "Title",
        required=True,
        max_chars=80,
        default=detail.record.title,
    )
    if title is None:
        return
    description = _prompt_limited_text(
        console,
        "Description",
        required=False,
        max_chars=240,
        default=detail.record.description,
    )
    if description is None:
        return
    surface.edit_asset(detail.record.id, title=title, description=description)
    console.print(
        "Title/description changed. Comprehension reflects previous metadata. "
        "Use 'Refresh comprehension' to update."
    )


def _prompt_path(*, console: Console, root: Path) -> Path | None:
    try:
        raw = typer.prompt("Path", default="", show_default=False)
    except (EOFError, KeyboardInterrupt):
        console.print("")
        return None
    if not str(raw).strip():
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        console.print("[red]Asset path must stay inside the workspace root.[/red]")
        return None
    try:
        raw_bytes = candidate.read_bytes()
    except OSError as exc:
        console.print(f"[red]Cannot read asset file: {exc}[/red]")
        return None
    try:
        _classify_asset(candidate, raw_bytes)
    except AssetError as exc:
        console.print(f"[red]{exc}[/red]")
        return None
    return candidate


def _prompt_limited_text(
    console: Console,
    label: str,
    *,
    required: bool,
    max_chars: int,
    default: str = "",
) -> str | None:
    while True:
        try:
            value = str(typer.prompt(label, default=default, show_default=bool(default))).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("")
            return None
        if not value and required:
            console.print(f"[red]{label} is required.[/red]")
            continue
        if len(value) > max_chars:
            console.print(f"[red]{label} must be {max_chars} characters or fewer.[/red]")
            continue
        return value


def _prompt_choice(
    *,
    console: Console,
    title: str,
    choices: list[tuple[str, str]],
) -> str | None:
    error_message: str | None = None
    while True:
        console.print()
        console.print(f"[bold]{title}[/bold]")
        if error_message:
            console.print(f"[red]{error_message}[/red]")
        for index, (_value, label) in enumerate(choices, start=1):
            console.print(f"  {index}) {label}")
        try:
            raw = str(typer.prompt("Choice", default="", show_default=False)).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("")
            return None
        if not raw:
            return None
        if raw.lower() in {"q", "quit", "cancel", "back"}:
            return None
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(choices):
                return choices[index][0]
        for value, label in choices:
            if raw.lower() in {value.lower(), label.lower()}:
                return value
        error_message = "Unknown choice."


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"
