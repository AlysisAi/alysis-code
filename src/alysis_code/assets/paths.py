from __future__ import annotations

from pathlib import Path

from ..forge import RunPaths


def asset_store_dir(run_paths: RunPaths) -> Path:
    return run_paths.asset_store_dir


def asset_raw_dir(run_paths: RunPaths, asset_id: str) -> Path:
    return run_paths.assets_raw_dir / asset_id


def asset_raw_path(run_paths: RunPaths, asset_id: str, original_filename: str) -> Path:
    return asset_raw_dir(run_paths, asset_id) / original_filename


def asset_extracted_text_path(run_paths: RunPaths, asset_id: str) -> Path:
    return run_paths.assets_extracted_dir / f"{asset_id}.txt"


def asset_thumbnail_path(run_paths: RunPaths, asset_id: str) -> Path:
    return run_paths.assets_extracted_dir / f"{asset_id}.thumb.png"


def asset_preview_path(run_paths: RunPaths, asset_id: str) -> Path:
    return run_paths.assets_extracted_dir / f"{asset_id}.preview.png"


def asset_comprehension_dir(run_paths: RunPaths, asset_id: str) -> Path:
    return run_paths.assets_comprehensions_dir / asset_id


def asset_comprehension_version_path(
    run_paths: RunPaths,
    asset_id: str,
    version: int,
) -> Path:
    return asset_comprehension_dir(run_paths, asset_id) / f"v{version}.json"


def asset_comprehension_current_path(run_paths: RunPaths, asset_id: str) -> Path:
    return asset_comprehension_dir(run_paths, asset_id) / "current.json"


def repo_rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
