from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ...assets.index import AssetIndex
from ...assets.models import AssetRecord

_MIGRATED_INDEX_RETRY_ATTEMPTS = 5
_MIGRATED_INDEX_RETRY_SECONDS = 0.02


@dataclass(frozen=True)
class ForgeAssetViewEntry:
    source: Literal["indexed", "legacy"]
    stored_path: str
    size_bytes: int | None
    display_name: str
    asset_id: str | None = None


def forge_asset_view_entries(paths: Any, plan: dict[str, Any]) -> list[ForgeAssetViewEntry]:
    indexed_records = _indexed_asset_records(
        paths,
        retry_on_empty=_legacy_migration_completed(plan),
    )
    migrated_legacy_paths = _migrated_legacy_paths(indexed_records)
    entries = [_indexed_entry(record) for record in indexed_records]
    entries.extend(
        entry
        for entry in _legacy_entries(plan)
        if not entry.stored_path or entry.stored_path not in migrated_legacy_paths
    )
    return entries


def forge_asset_view_count(paths: Any, plan: dict[str, Any]) -> int:
    return len(forge_asset_view_entries(paths, plan))


def _indexed_asset_records(
    paths: Any,
    *,
    retry_on_empty: bool = False,
) -> list[AssetRecord]:
    if not hasattr(paths, "assets_index_path"):
        return []
    attempts = _MIGRATED_INDEX_RETRY_ATTEMPTS if retry_on_empty else 1
    for attempt in range(attempts):
        try:
            records = AssetIndex(paths).records(include_deleted=False)
        except Exception:
            records = []
        if records or not retry_on_empty or attempt >= attempts - 1:
            return records
        time.sleep(_MIGRATED_INDEX_RETRY_SECONDS)
    return []


def _legacy_migration_completed(plan: dict[str, Any]) -> bool:
    if not isinstance(plan, dict):
        return False
    return bool(str(plan.get("legacy_assets_migrated_at") or "").strip())


def _indexed_entry(record: AssetRecord) -> ForgeAssetViewEntry:
    display_name = record.original_filename.strip() or _path_name(record.stored_path)
    return ForgeAssetViewEntry(
        source="indexed",
        stored_path=record.stored_path,
        size_bytes=record.size_bytes,
        display_name=display_name,
        asset_id=record.id,
    )


def _legacy_entries(plan: dict[str, Any]) -> list[ForgeAssetViewEntry]:
    legacy_assets = plan.get("assets") if isinstance(plan, dict) else None
    if not isinstance(legacy_assets, list):
        return []
    entries: list[ForgeAssetViewEntry] = []
    for asset in legacy_assets:
        if not isinstance(asset, dict):
            continue
        stored_path = str(asset.get("stored_path") or "").strip()
        display_name = _path_name(stored_path)
        entries.append(
            ForgeAssetViewEntry(
                source="legacy",
                stored_path=stored_path,
                size_bytes=_optional_int(asset.get("size_bytes")),
                display_name=display_name,
                asset_id=None,
            )
        )
    return entries


def _migrated_legacy_paths(records: list[AssetRecord]) -> set[str]:
    paths: set[str] = set()
    for record in records:
        raw = record.added_by.get("legacy_stored_path")
        if isinstance(raw, str) and raw.strip():
            paths.add(raw.strip())
    return paths


def _path_name(path: str) -> str:
    value = str(path or "").strip()
    return Path(value).name if value else ""


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
