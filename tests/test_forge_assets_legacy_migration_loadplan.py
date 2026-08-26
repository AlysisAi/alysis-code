from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.assets.index import AssetIndex
from alysis_code.atomic_io import atomic_write_json
from alysis_code.cli_impl.assets_modal import run_assets_modal
from alysis_code.config import AppConfig
from alysis_code.forge import ForgeError, create_plan_run, load_plan, save_plan


def _legacy_asset(paths, name: str, text: str) -> dict[str, object]:
    path = paths.assets_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {
        "original_path": name,
        "stored_path": path.relative_to(paths.root).as_posix(),
        "size_bytes": path.stat().st_size,
        "added_at": "2026-05-04T00:00:00+00:00",
    }


def test_load_plan_triggers_legacy_asset_migration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths, migrate_legacy=False)
    plan["schema_version"] = 1
    plan["assets"] = [_legacy_asset(paths, "legacy.txt", "legacy\n")]
    save_plan(paths, plan)
    cfg = AppConfig(model="fake-model")
    cfg.assets.enabled = False

    monkeypatch.setattr("alysis_code.config.load_config", lambda: cfg)

    migrated = load_plan(paths)

    assert migrated["schema_version"] == 2
    assert migrated["assets"] == []
    assert migrated["legacy_assets_migrated_at"]


def test_load_plan_v2_is_noop_without_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    def fail_load_config() -> AppConfig:
        raise AssertionError("v2 load should not check migration config")

    monkeypatch.setattr("alysis_code.config.load_config", fail_load_config)

    loaded = load_plan(paths)

    assert loaded["schema_version"] == 2


def test_load_plan_rejects_malformed_legacy_assets_without_migrating(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths, migrate_legacy=False)
    plan["schema_version"] = 1
    plan["assets"] = {"stored_path": "not-an-array"}
    atomic_write_json(paths.plan_json_path, plan)
    cfg = AppConfig(model="fake-model")
    cfg.assets.enabled = False

    monkeypatch.setattr("alysis_code.config.load_config", lambda: cfg)

    with pytest.raises(ForgeError, match="'assets' must be an array"):
        load_plan(paths)

    loaded = json.loads(paths.plan_json_path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert loaded["assets"] == {"stored_path": "not-an-array"}


def test_assets_modal_triggers_legacy_asset_migration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths, migrate_legacy=False)
    plan["schema_version"] = 1
    plan["assets"] = [_legacy_asset(paths, "modal.txt", "modal\n")]
    save_plan(paths, plan)
    cfg = AppConfig(model="fake-model")
    cfg.assets.enabled = False
    monkeypatch.setattr("alysis_code.config.load_config", lambda: cfg)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    stream = io.StringIO()

    run_assets_modal(
        cfg=cfg,
        run_paths=paths,
        console=Console(file=stream, force_terminal=False),
    )

    migrated = load_plan(paths, migrate_legacy=False)
    assert migrated["schema_version"] == 2
    assert migrated["assets"] == []
    assert len(AssetIndex(paths).records()) == 1
