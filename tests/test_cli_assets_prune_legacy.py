from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from alysis_code.assets import AssetSurface
from alysis_code.assets.legacy_migration import migrate_legacy_assets
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run, load_plan, save_plan


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


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


def _write_v1(paths, assets: list[dict[str, object]]) -> None:
    plan = load_plan(paths, migrate_legacy=False)
    plan["schema_version"] = 1
    plan["assets"] = assets
    plan.pop("legacy_assets_migrated_at", None)
    save_plan(paths, plan)


def test_prune_legacy_refuses_v1_plan(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    _write_v1(paths, [_legacy_asset(paths, "legacy.txt", "legacy\n")])

    result = runner.invoke(
        alysis_app,
        ["forge", "assets", "prune-legacy", "--path", os.fspath(repo), "--yes"],
        env=_env(tmp_path),
    )

    assert result.exit_code != 0
    assert "requires plan schema_version=2" in result.output


def test_prune_legacy_deletes_verified_files(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    legacy = _legacy_asset(paths, "legacy.txt", "legacy\n")
    legacy_path = paths.root / str(legacy["stored_path"])
    _write_v1(paths, [legacy])
    cfg = AppConfig(model="fake-model")
    cfg.assets.enabled = False
    surface = AssetSurface(cfg=cfg, run_paths=paths)
    migrate_legacy_assets(cfg=cfg, run_paths=paths, surface=surface, comprehend_mode="skip")

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "prune-legacy",
            "--path",
            os.fspath(repo),
            "--yes",
            "--format",
            "json",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["deleted"] == [legacy["stored_path"]]
    assert not legacy_path.exists()


def test_prune_legacy_refuses_unverified_file(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    legacy = _legacy_asset(paths, "legacy.txt", "legacy\n")
    _write_v1(paths, [legacy])
    plan = load_plan(paths, migrate_legacy=False)
    plan["schema_version"] = 2
    plan["legacy_assets_migrated_at"] = "2026-05-04T00:00:00+00:00"
    save_plan(paths, plan)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "prune-legacy",
            "--path",
            os.fspath(repo),
            "--yes",
            "--format",
            "json",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["unverified"] == [legacy["stored_path"]]
