from __future__ import annotations

import json
from pathlib import Path

import pytest
from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import AssetSurface, mirror_task_assets
from alysis_code.assets.models import AssetError
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run


def _surface(tmp_path: Path) -> AssetSurface:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    return AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )


def _briefing(asset_id: str) -> dict[str, object]:
    return {
        "asset_briefing": {
            "primary": [
                {
                    "asset_id": asset_id,
                    "rationale": "Needed for implementation.",
                    "expected_use": "Read before editing.",
                }
            ],
            "may_need": [],
        }
    }


def test_mirror_copies_primary_may_need_and_pinned_assets(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    primary = surface.add_asset(
        write_text_asset_source(tmp_path, "primary.txt", "primary"),
        title="Primary",
        comprehend="sync",
    ).record
    may_need = surface.add_asset(
        write_text_asset_source(tmp_path, "maybe.txt", "maybe"),
        title="Maybe",
        comprehend="sync",
    ).record
    pinned = surface.add_asset(
        write_text_asset_source(tmp_path, "pinned.txt", "pinned"),
        title="Pinned",
        pinned=True,
        comprehend="sync",
    ).record
    workspace = tmp_path / "work"
    workspace.mkdir()
    task = {
        "id": "T01",
        "asset_briefing": {
            "primary": [
                {"asset_id": primary.id, "rationale": "r", "expected_use": "u"},
            ],
            "may_need": [
                {"asset_id": may_need.id, "rationale": "r2", "expected_use": "u2"},
            ],
        },
    }

    mirror = mirror_task_assets(
        task=task, plan={"tasks": [task]}, surface=surface, workspace_path=workspace
    )

    assert mirror.manifest_path.exists()
    assert mirror.primary[0].raw_workspace_path is not None
    assert mirror.primary[0].raw_workspace_path.read_text(encoding="utf-8") == "primary"
    assert mirror.may_need[0].asset_id == may_need.id
    assert mirror.pinned[0].asset_id == pinned.id
    manifest = json.loads(mirror.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["primary"][0]["comprehension_version"] == 1


def test_mirror_infers_primary_asset_for_attached_spec_task(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "feature_spec.md", "Add a greeting.\n"),
        title="Feature spec",
        comprehend="sync",
    ).record
    workspace = tmp_path / "work"
    workspace.mkdir()
    task = {
        "id": "T01",
        "title": "Update README.md using attached feature spec",
        "description": "Manual planning chat task: Update README.md using attached feature spec",
        "acceptance_criteria": [],
        "dependencies": [],
        "estimated_files": ["README.md"],
        "write_scope": ["README.md"],
    }

    mirror = mirror_task_assets(
        task=task, plan={"tasks": [task]}, surface=surface, workspace_path=workspace
    )

    assert mirror.primary[0].asset_id == record.id
    assert mirror.primary[0].raw_workspace_path is not None
    assert mirror.primary[0].raw_workspace_path.read_text(encoding="utf-8") == "Add a greeting.\n"


def test_mirror_records_tombstoned_reference_without_copying(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "content"),
        title="Old",
        comprehend="sync",
    ).record
    surface.delete_asset(record.id)
    workspace = tmp_path / "work"
    workspace.mkdir()

    mirror = mirror_task_assets(
        task={"id": "T01", **_briefing(record.id)},
        plan={},
        surface=surface,
        workspace_path=workspace,
    )

    assert mirror.primary[0].status == "deleted"
    assert mirror.primary[0].raw_workspace_path is None


def test_mirror_missing_source_respects_fail_on_mirror_error(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "content"),
        title="Asset",
        comprehend="sync",
    ).record
    (surface.run_paths.root / record.stored_path).unlink()
    workspace = tmp_path / "work"
    workspace.mkdir()

    mirror = mirror_task_assets(
        task={"id": "T01", **_briefing(record.id)},
        plan={},
        surface=surface,
        workspace_path=workspace,
    )
    assert mirror.primary[0].status == "missing"

    surface.cfg.assets.worker.fail_on_mirror_error = True
    with pytest.raises(AssetError):
        mirror_task_assets(
            task={"id": "T01", **_briefing(record.id)},
            plan={},
            surface=surface,
            workspace_path=workspace,
        )


def test_mirror_freezes_current_comprehension_version(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "content"),
        title="Asset",
        comprehend="sync",
    ).record
    workspace = tmp_path / "work"
    workspace.mkdir()
    mirror = mirror_task_assets(
        task={"id": "T01", **_briefing(record.id)},
        plan={},
        surface=surface,
        workspace_path=workspace,
    )

    surface.refresh_comprehension(record.id, mode="sync")

    frozen = json.loads(mirror.primary[0].comprehension_workspace_path.read_text(encoding="utf-8"))
    assert frozen["version"] == 1


def test_mirror_cleans_staging_on_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "content"),
        title="Asset",
        comprehend="sync",
    ).record
    workspace = tmp_path / "work"
    workspace.mkdir()

    def fail_copy(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("copy failed")

    monkeypatch.setattr("alysis_code.assets.worker_mirror.shutil.copy2", fail_copy)

    with pytest.raises(AssetError):
        mirror_task_assets(
            task={"id": "T01", **_briefing(record.id)},
            plan={},
            surface=surface,
            workspace_path=workspace,
        )

    assert not (workspace / ".alysis" / "task_assets").exists()
    assert not list((workspace / ".alysis").glob("task_assets.staging.*"))
