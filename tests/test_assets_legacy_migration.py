from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from _assets_test_helpers import FakeAssetComprehender

from alysis_code.assets import AssetError, AssetSurface
from alysis_code.assets.legacy_migration import (
    LegacyMigrationLock,
    migrate_legacy_assets,
)
from alysis_code.atomic_io import atomic_write_json
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run, load_plan, save_plan


def _cfg(*, enabled: bool) -> AppConfig:
    cfg = AppConfig(model="fake-model")
    cfg.assets.enabled = enabled
    return cfg


def _legacy_file(paths, name: str, text: str) -> tuple[Path, dict[str, object]]:
    path = paths.assets_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, {
        "original_path": name,
        "stored_path": path.relative_to(paths.root).as_posix(),
        "size_bytes": path.stat().st_size,
        "added_at": "2026-05-04T00:00:00+00:00",
    }


def _write_v1_plan(paths, assets: list[dict[str, object]]) -> None:
    plan = load_plan(paths, migrate_legacy=False)
    plan["schema_version"] = 1
    plan["assets"] = assets
    plan.pop("legacy_assets_migrated_at", None)
    save_plan(paths, plan)


def test_migrate_v1_legacy_assets_to_new_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    _, first = _legacy_file(paths, "alpha.txt", "alpha\n")
    _, second = _legacy_file(paths, "beta.txt", "beta\n")
    _write_v1_plan(paths, [first, second])
    cfg = _cfg(enabled=False)
    surface = AssetSurface(cfg=cfg, run_paths=paths)

    result = migrate_legacy_assets(
        cfg=cfg,
        run_paths=paths,
        surface=surface,
        comprehend_mode="async",
    )

    assert result.schema_version_before == 1
    assert result.schema_version_after == 2
    assert [item.title for item in result.migrated_assets] == ["Alpha", "Beta"]
    assert result.plan_assets_array_cleared is True
    assert result.plan_v2_written is True
    loaded = load_plan(paths, migrate_legacy=False)
    assert loaded["schema_version"] == 2
    assert loaded["assets"] == []
    assert loaded["legacy_assets_migrated_at"]
    assert len(surface.index.records()) == 2


def test_migrate_v2_plan_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    cfg = _cfg(enabled=False)
    surface = AssetSurface(cfg=cfg, run_paths=paths)

    result = migrate_legacy_assets(
        cfg=cfg,
        run_paths=paths,
        surface=surface,
        comprehend_mode="skip",
    )

    assert result.schema_version_before == 2
    assert result.plan_v2_written is False
    assert result.migrated_assets == []


def test_migration_failure_leaves_plan_v1_for_retry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    _, good = _legacy_file(paths, "good.txt", "good\n")
    bad = {
        "original_path": "missing.txt",
        "stored_path": ".alysis/runs/missing/plan/assets/missing.txt",
        "size_bytes": 1,
        "added_at": "2026-05-04T00:00:00+00:00",
    }
    _write_v1_plan(paths, [good, bad])
    cfg = _cfg(enabled=False)
    surface = AssetSurface(cfg=cfg, run_paths=paths)

    result = migrate_legacy_assets(
        cfg=cfg,
        run_paths=paths,
        surface=surface,
        comprehend_mode="skip",
    )

    assert len(result.migrated_assets) == 1
    assert result.failed
    assert result.plan_v2_written is False
    assert load_plan(paths, migrate_legacy=False)["schema_version"] == 1


def test_migration_rejects_malformed_legacy_assets_array(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths, migrate_legacy=False)
    plan["schema_version"] = 1
    plan["assets"] = {"stored_path": "not-an-array"}
    atomic_write_json(paths.plan_json_path, plan)
    cfg = _cfg(enabled=False)
    surface = AssetSurface(cfg=cfg, run_paths=paths)

    with pytest.raises(AssetError, match="'assets' must be an array"):
        migrate_legacy_assets(
            cfg=cfg,
            run_paths=paths,
            surface=surface,
            comprehend_mode="skip",
        )

    loaded = json.loads(paths.plan_json_path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert loaded["assets"] == {"stored_path": "not-an-array"}


def test_migration_lock_waits_and_observes_already_migrated_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    _, legacy = _legacy_file(paths, "locked.txt", "locked\n")
    _write_v1_plan(paths, [legacy])
    cfg = _cfg(enabled=False)
    surface = AssetSurface(cfg=cfg, run_paths=paths)
    results = []

    def run_migration() -> None:
        results.append(
            migrate_legacy_assets(
                cfg=cfg,
                run_paths=paths,
                surface=surface,
                comprehend_mode="skip",
            )
        )

    with LegacyMigrationLock(paths):
        thread = threading.Thread(target=run_migration)
        thread.start()
        time.sleep(0.1)
        assert thread.is_alive()
        plan = load_plan(paths, migrate_legacy=False)
        plan["schema_version"] = 2
        plan["assets"] = []
        plan["legacy_assets_migrated_at"] = "2026-05-04T00:00:00+00:00"
        save_plan(paths, plan)
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert results[0].schema_version_before == 2
    assert results[0].plan_v2_written is False


def test_windows_stale_migration_lock_probe_does_not_send_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    lock_path = paths.run_dir / "legacy_migration.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 9_999_999,
                "hostname": socket.gethostname(),
                "created_epoch": time.time() - 120.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fail_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("Windows migration lock probes must not call os.kill")

    monkeypatch.setattr("alysis_code.assets.legacy_migration.os.name", "nt")
    monkeypatch.setattr("alysis_code.assets.legacy_migration.os.kill", fail_kill)
    monkeypatch.setattr(
        "alysis_code.assets.legacy_migration._windows_process_is_running",
        lambda _pid: False,
    )

    with LegacyMigrationLock(paths):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()


def test_plan_write_failure_dedupes_on_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    _, legacy = _legacy_file(paths, "retry.txt", "retry\n")
    _write_v1_plan(paths, [legacy])
    cfg = _cfg(enabled=False)
    surface = AssetSurface(cfg=cfg, run_paths=paths)

    import alysis_code.forge as forge_mod

    real_save_plan = forge_mod.save_plan

    def fail_save(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        raise OSError("simulated write failure")

    monkeypatch.setattr(forge_mod, "save_plan", fail_save)
    with pytest.raises(OSError, match="simulated write failure"):
        migrate_legacy_assets(
            cfg=cfg,
            run_paths=paths,
            surface=surface,
            comprehend_mode="skip",
        )
    assert len(surface.index.records()) == 1
    assert load_plan(paths, migrate_legacy=False)["schema_version"] == 1

    monkeypatch.setattr(forge_mod, "save_plan", real_save_plan)
    result = migrate_legacy_assets(
        cfg=cfg,
        run_paths=paths,
        surface=surface,
        comprehend_mode="skip",
    )

    assert result.migrated_assets == []
    assert result.skipped_existing == [legacy["stored_path"]]
    assert load_plan(paths, migrate_legacy=False)["schema_version"] == 2


def test_migration_async_starts_background_comprehension(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    _, asset = _legacy_file(paths, "slow.txt", "slow\n")
    _write_v1_plan(paths, [asset])
    cfg = _cfg(enabled=True)
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths, delay_seconds=0.2),  # type: ignore[arg-type]
    )

    result = migrate_legacy_assets(
        cfg=cfg,
        run_paths=paths,
        surface=surface,
        comprehend_mode="async",
    )

    assert result.migrated_assets[0].comprehension_handle_running is True
    surface.join_pending(timeout_seconds=2.0)
