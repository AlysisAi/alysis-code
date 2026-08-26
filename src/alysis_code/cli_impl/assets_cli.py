from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..assets import (
    AssetAlreadyExistsError,
    AssetError,
    AssetSurface,
    AssetSurfaceDetail,
    AssetSurfaceEntry,
    asset_reference_check,
    bind_asset_to_matching_tasks,
)
from ..assets.surface import build_asset_surface
from ..config import AppConfig, load_config
from ..forge import ForgeError, RunPaths, load_current_run_paths, load_plan, save_plan
from ..surface.console import make_console

assets_app = typer.Typer(add_completion=False, help="Manage first-class forge assets.")


@assets_app.command("list")
def assets_list(
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    include_deleted: bool = typer.Option(
        False,
        "--include-deleted",
        help="Include tombstoned assets.",
    ),
    output_format: str = typer.Option(
        "table",
        "--format",
        help="Output format: table or json.",
    ),
) -> None:
    surface = _load_surface_or_exit(path)
    entries = surface.list_assets(include_deleted=include_deleted)
    fmt = _normalize_format(output_format, allowed={"table", "json"})
    if fmt == "json":
        _print_json(
            {
                "run_id": surface.run_paths.run_id,
                "assets": [_entry_payload(entry) for entry in entries],
            }
        )
        return
    table = Table(title=f"forge assets · {surface.run_paths.run_id}")
    table.add_column("status")
    table.add_column("kind")
    table.add_column("pinned")
    table.add_column("id")
    table.add_column("title")
    table.add_column("size")
    for entry in entries:
        table.add_row(
            entry.comprehension_status,
            entry.record.kind,
            "yes" if entry.record.pinned else "no",
            entry.record.id,
            entry.record.title,
            _format_size(entry.record.size_bytes),
        )
    _console().print(table)


@assets_app.command("show")
def assets_show(
    asset_id: str = typer.Argument(..., help="Asset id."),
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format: text or json."),
) -> None:
    surface = _load_surface_or_exit(path)
    try:
        detail = surface.show_asset(asset_id)
    except AssetError as exc:
        _asset_error(str(exc))
    fmt = _normalize_format(output_format, allowed={"text", "json"})
    if fmt == "json":
        _print_json(_detail_payload(detail))
        return
    record = detail.record
    status = "deleted" if record.deleted_at else detail.comprehension_status
    _console().print(f"Asset {record.id}")
    _console().print(f"- title: {record.title}")
    _console().print(f"- status: {status}")
    _console().print(f"- kind: {record.kind}")
    _console().print(f"- size: {record.size_bytes} bytes")
    _console().print(f"- pinned: {'yes' if record.pinned else 'no'}")
    _console().print(f"- original file: {record.original_filename}")
    if detail.comprehension is not None:
        _console().print(f"- source: {detail.comprehension.source}")
        _console().print(f"- detected language: {detail.comprehension.detected_language or '-'}")
        summary = detail.comprehension.data.semantic_summary.strip()
        if summary:
            _console().print(f"- summary: {summary}")
    if detail.versions:
        _console().print(f"- versions: {', '.join(str(version) for version in detail.versions)}")


@assets_app.command("add")
def assets_add(
    file: Path = typer.Argument(..., help="File to ingest."),
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    title: str | None = typer.Option(None, "--title", help="Asset title."),
    description: str = typer.Option("", "--description", help="Asset description."),
    pinned: bool = typer.Option(False, "--pinned", help="Pin asset to all tasks."),
    wait: bool = typer.Option(False, "--wait", help="Wait for comprehension to complete."),
    link: bool = typer.Option(False, "--link", help="Return existing asset on dedupe collision."),
) -> None:
    clean_title = _resolve_title(title)
    surface = _load_surface_or_exit(path)
    if wait:
        _require_subscription_comprehension(surface.cfg)
    try:
        result = surface.add_asset(
            file,
            title=clean_title,
            description=description,
            pinned=pinned,
            added_by={"phase": "cli", "command": "forge assets add"},
            comprehend="sync" if wait else "skip",
            dedupe_policy="link" if link else "reject",
        )
    except AssetAlreadyExistsError as exc:
        _asset_error(f"{exc} existing_id={exc.existing_id}")
    except AssetError as exc:
        _asset_error(str(exc))
    record = result.record
    _console().print(f"Asset: {record.id}")
    _console().print(f"- title: {record.title}")
    _console().print(f"- kind: {record.kind}")
    _console().print(f"- pinned: {'yes' if record.pinned else 'no'}")
    if result.comprehension_record is not None:
        _console().print(f"- comprehension: {result.comprehension_record.status}")
    elif wait:
        _console().print(f"- comprehension: {record.comprehension_status}")
    else:
        _console().print("- comprehension: pending")
        _console().print(f"- next: alysis forge assets refresh {record.id} --path {path} --wait")
    bound_task_ids = _bind_asset_to_current_plan(surface=surface, record=record)
    if bound_task_ids:
        _console().print("- bound tasks: " + ", ".join(bound_task_ids))


@assets_app.command("delete")
def assets_delete(
    asset_id: str = typer.Argument(..., help="Asset id."),
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    if not yes:
        if not sys.stdin.isatty():
            _asset_error("--yes is required when stdin is not a terminal")
        if not typer.confirm(f"Delete asset {asset_id}?", default=False):
            _console().print("Cancelled.")
            return
    surface = _load_surface_or_exit(path)
    try:
        deleted = surface.delete_asset(asset_id)
    except AssetError as exc:
        _asset_error(str(exc))
    _console().print(f"Deleted asset: {deleted.id}")


@assets_app.command("edit")
def assets_edit(
    asset_id: str = typer.Argument(..., help="Asset id."),
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    title: str | None = typer.Option(None, "--title", help="New title."),
    description: str | None = typer.Option(None, "--description", help="New description."),
    pinned: bool | None = typer.Option(None, "--pin/--unpin", help="Set pinned state."),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh comprehension after edit."),
) -> None:
    if title is None and description is None and pinned is None and not refresh:
        _asset_error("Nothing to edit. Use --title, --description, --pin, --unpin, or --refresh.")
    surface = _load_surface_or_exit(path)
    if refresh:
        _require_subscription_comprehension(surface.cfg)
    try:
        detail = surface.edit_asset(
            asset_id,
            title=title,
            description=description,
            pinned=pinned,
            retrigger_comprehension=False,
        )
        refreshed_record = (
            surface.refresh_comprehension(asset_id, mode="sync").join() if refresh else None
        )
    except AssetError as exc:
        _asset_error(str(exc))
    _console().print(f"Updated asset: {detail.record.id}")
    _console().print(f"- pinned: {'yes' if detail.record.pinned else 'no'}")
    if refreshed_record is not None:
        _console().print(f"- comprehension: {refreshed_record.status}")


@assets_app.command("refresh")
def assets_refresh(
    asset_id: str = typer.Argument(..., help="Asset id."),
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    wait: bool = typer.Option(False, "--wait", help="Wait for comprehension to complete."),
) -> None:
    surface = _load_surface_or_exit(path)
    if not wait:
        _asset_error(
            "CLI refresh requires --wait. Use /assets for live background refresh in chat."
        )
    _require_subscription_comprehension(surface.cfg)
    try:
        handle = surface.refresh_comprehension(asset_id, mode="sync")
    except AssetError as exc:
        _asset_error(str(exc))
    record = handle.join() if wait else None
    _console().print(f"Refresh started: {asset_id}")
    if record is not None:
        _console().print(f"- comprehension: {record.status}")


@assets_app.command("cancel-pending")
def assets_cancel_pending(
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
) -> None:
    _ = _load_surface_or_exit(path)
    _console().print(
        "No persistent CLI background comprehensions are running. "
        "Use /assets to cancel live modal refreshes."
    )


@assets_app.command("check-plan")
def assets_check_plan(
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    output_format: str = typer.Option("text", "--format", help="Output format: text or json."),
) -> None:
    surface = _load_surface_or_exit(path)
    fmt = _normalize_format(output_format, allowed={"text", "json"})
    try:
        plan = load_plan(surface.run_paths)
    except ForgeError as exc:
        _asset_error(str(exc))
    report = asset_reference_check(plan, surface)
    payload = {
        "deleted_referenced": [
            {"task_id": task_id, "asset_id": asset_id}
            for task_id, asset_id in report.deleted_referenced
        ],
        "missing_referenced": [
            {"task_id": task_id, "asset_id": asset_id}
            for task_id, asset_id in report.missing_referenced
        ],
        "pinned_added": report.pinned_added,
    }
    if fmt == "json":
        _print_json(payload)
    else:
        _console().print("Plan asset reference check")
        _console().print(f"- deleted references: {len(report.deleted_referenced)}")
        _console().print(f"- missing references: {len(report.missing_referenced)}")
        _console().print(f"- pinned assets not bound: {len(report.pinned_added)}")
    if report.deleted_referenced or report.missing_referenced:
        raise typer.Exit(code=1)


@assets_app.command("prune-legacy")
def assets_prune_legacy(
    path: Path = typer.Option(
        Path("."), "--path", help="Workspace path or repository subdirectory."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
    output_format: str = typer.Option("text", "--format", help="Output format: text or json."),
) -> None:
    fmt = _normalize_format(output_format, allowed={"text", "json"})
    try:
        cfg = load_config()
        run_paths = load_current_run_paths(path)
        plan = load_plan(run_paths, migrate_legacy=False)
        surface = build_surface(cfg=cfg, run_paths=run_paths)
    except (ForgeError, AssetError) as exc:
        _asset_error(str(exc))
    schema_version = _plan_schema_version(plan)
    if schema_version < 2:
        _asset_error("Legacy asset pruning requires plan schema_version=2 after migration.")
    legacy_files = _legacy_asset_files(run_paths)
    known_hashes = {
        record.sha256
        for record in surface.index.records(include_deleted=True)
        if str(record.sha256 or "").strip()
    }
    verified: list[str] = []
    unverified: list[str] = []
    for file_path in legacy_files:
        digest = _sha256_file(file_path)
        rel = file_path.relative_to(run_paths.root).as_posix()
        if digest in known_hashes:
            verified.append(rel)
        else:
            unverified.append(rel)
    payload = {
        "run_id": run_paths.run_id,
        "verified": verified,
        "unverified": unverified,
        "deleted": [],
    }
    if unverified:
        if fmt == "json":
            _print_json(payload)
        else:
            _console().print("Refusing to prune unverified legacy asset files:")
            for item in unverified:
                _console().print(f"- {item}")
        raise typer.Exit(code=1)
    if not verified:
        if fmt == "json":
            _print_json(payload)
        else:
            _console().print("No legacy asset files to prune.")
        return
    if not yes:
        if not sys.stdin.isatty():
            _asset_error("--yes is required when stdin is not a terminal")
        if not typer.confirm(
            f"Delete {len(verified)} verified legacy asset file(s)?", default=False
        ):
            _console().print("Cancelled.")
            return
    deleted: list[str] = []
    for file_path in legacy_files:
        rel = file_path.relative_to(run_paths.root).as_posix()
        if rel not in verified:
            continue
        try:
            file_path.unlink()
        except OSError as exc:
            _asset_error(f"Failed to delete {rel}: {exc}")
        deleted.append(rel)
    _remove_empty_legacy_dirs(run_paths)
    payload["deleted"] = deleted
    if fmt == "json":
        _print_json(payload)
    else:
        _console().print(f"Pruned {len(deleted)} verified legacy asset file(s).")


def _load_surface_or_exit(path: Path) -> AssetSurface:
    try:
        cfg = load_config()
        run_paths = load_current_run_paths(path)
        load_plan(run_paths)
        return build_surface(cfg=cfg, run_paths=run_paths)
    except (ForgeError, AssetError) as exc:
        _asset_error(str(exc))


def build_surface(*, cfg: AppConfig, run_paths: RunPaths) -> AssetSurface:
    return build_asset_surface(cfg=cfg, run_paths=run_paths)


def _require_subscription_comprehension(cfg: AppConfig) -> None:
    from ..profiles import active_subscription_selection_ready, get_active_profile

    try:
        profile = get_active_profile(cfg)
    except Exception:
        return
    provider_id = str(profile.auth_provider or "").strip()
    if not provider_id:
        return
    if not active_subscription_selection_ready(cfg):
        _asset_error(
            "Choose the subscription model and reasoning effort in "
            "/config → Default Model before running asset comprehension."
        )
    try:
        from ..provider_auth import create_provider_auth

        adapter = create_provider_auth(provider_id)
        compatible = profile.protocol == adapter.protocol and profile.base_url.rstrip(
            "/"
        ) == adapter.base_url.rstrip("/")
        status = adapter.account_status()
    except Exception as exc:  # noqa: BLE001
        _asset_error(f"Subscription status check failed: {exc}")
    if not compatible:
        _asset_error("The subscription profile is incompatible; reconnect it through setup.")
    if not status.connected:
        detail = f" ({status.detail})" if status.detail else ""
        _asset_error(
            f"The selected AI subscription is not connected{detail}. "
            f"Run `alysis auth login {provider_id}`."
        )


def _bind_asset_to_current_plan(*, surface: AssetSurface, record: Any) -> list[str]:
    try:
        plan = load_plan(surface.run_paths)
        active_records = surface.index.records(include_deleted=False)
        bound_task_ids = bind_asset_to_matching_tasks(
            plan=plan,
            record=record,
            active_records=active_records,
        )
        if bound_task_ids:
            save_plan(surface.run_paths, plan)
        return bound_task_ids
    except (ForgeError, AssetError):
        return []


def _resolve_title(title: str | None) -> str:
    if title is None:
        if not sys.stdin.isatty():
            _asset_error("--title is required when stdin is not a terminal")
        title = typer.prompt("Title")
    clean = str(title or "").strip()
    if not clean:
        _asset_error("Asset title is required.")
    return clean


def _normalize_format(value: str, *, allowed: set[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        _asset_error(f"--format must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _entry_payload(entry: AssetSurfaceEntry) -> dict[str, Any]:
    return {
        "record": entry.record.to_dict(),
        "comprehension_status": entry.comprehension_status,
        "comprehension_source": entry.comprehension_source,
        "comprehension_summary_preview": entry.comprehension_summary_preview,
        "detected_language": entry.detected_language,
    }


def _detail_payload(detail: AssetSurfaceDetail) -> dict[str, Any]:
    return {
        "record": detail.record.to_dict(),
        "comprehension_status": detail.comprehension_status,
        "comprehension": (
            detail.comprehension.to_dict() if detail.comprehension is not None else None
        ),
        "versions": detail.versions,
        "extracted_text_preview": detail.extracted_text_preview,
    }


def _print_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _console() -> Console:
    return make_console()


def _asset_error(message: str) -> None:
    typer.echo(f"Asset error: {message}", err=True)
    raise typer.Exit(code=2)


def _plan_schema_version(plan: dict[str, Any]) -> int:
    try:
        return int(plan.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _legacy_asset_files(run_paths: RunPaths) -> list[Path]:
    roots = [run_paths.assets_dir, run_paths.assets_text_dir]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(path for path in sorted(root.rglob("*")) if path.is_file())
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_empty_legacy_dirs(run_paths: RunPaths) -> None:
    for root in (run_paths.assets_text_dir, run_paths.assets_dir):
        if not root.exists():
            continue
        for directory in sorted(
            [path for path in root.rglob("*") if path.is_dir()],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass
