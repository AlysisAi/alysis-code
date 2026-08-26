from __future__ import annotations

import json
import os
import time
from pathlib import Path

from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source
from typer.testing import CliRunner

from alysis_code.assets import AssetSurface
from alysis_code.assets.index import AssetIndex
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run, save_plan


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _patch_surface_builder(monkeypatch, *, delay_seconds: float = 0.0) -> None:
    from alysis_code.cli_impl import assets_cli

    def fake_build_surface(*, cfg: AppConfig, run_paths):
        return AssetSurface(
            cfg=cfg,
            run_paths=run_paths,
            comprehender=FakeAssetComprehender(run_paths, delay_seconds=delay_seconds),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(assets_cli, "build_surface", fake_build_surface)


def test_assets_list_and_show_json(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    source = write_text_asset_source(repo, "brief.txt", "hello\n")
    record = surface.add_asset(source, title="Brief", comprehend="sync").record
    _patch_surface_builder(monkeypatch)

    list_result = runner.invoke(
        alysis_app,
        ["forge", "assets", "list", "--path", os.fspath(repo), "--format", "json"],
        env=_env(tmp_path),
    )
    show_result = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "show",
            record.id,
            "--path",
            os.fspath(repo),
            "--format",
            "json",
        ],
        env=_env(tmp_path),
    )

    assert list_result.exit_code == 0
    listed = json.loads(list_result.output)
    assert listed["assets"][0]["record"]["id"] == record.id
    assert listed["assets"][0]["comprehension_status"] == "ready"
    assert show_result.exit_code == 0
    shown = json.loads(show_result.output)
    assert shown["record"]["id"] == record.id
    assert shown["comprehension"]["data"]["semantic_summary"] == "Summary 1"


def test_assets_add_wait_blocks_until_comprehension_finishes(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    create_plan_run(repo)
    source = write_text_asset_source(repo, "brief.txt", "hello\n")
    _patch_surface_builder(monkeypatch, delay_seconds=0.05)

    started = time.monotonic()
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "add",
            os.fspath(source),
            "--path",
            os.fspath(repo),
            "--title",
            "Brief",
            "--wait",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert time.monotonic() - started >= 0.04
    assert "comprehension: ready" in result.output


def test_assets_add_binds_matching_existing_task(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    save_plan(
        paths,
        {
            "schema_version": 2,
            "run_id": paths.run_id,
            "created_at": "2026-05-03T00:00:00+00:00",
            "updated_at": "2026-05-03T00:00:00+00:00",
            "project_goal": "Use attached spec",
            "summary": "Use attached spec",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Update README.md using attached feature spec",
                    "description": "Manual planning chat task: Update README.md using attached feature spec",
                    "acceptance_criteria": [],
                    "dependencies": [],
                    "estimated_files": ["README.md"],
                    "write_scope": ["README.md"],
                    "status": "planned",
                    "attempts": 0,
                }
            ],
            "assets": [],
        },
    )
    source = write_text_asset_source(repo, "feature_spec.md", "Add a greeting.\n")
    _patch_surface_builder(monkeypatch)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "add",
            os.fspath(source),
            "--path",
            os.fspath(repo),
            "--title",
            "Feature spec",
            "--wait",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "bound tasks: T01" in result.output
    plan = json.loads(paths.plan_json_path.read_text(encoding="utf-8"))
    asset_id = AssetIndex(paths).records()[0].id
    assert plan["tasks"][0]["asset_briefing"]["primary"][0]["asset_id"] == asset_id


def test_assets_add_without_wait_stages_pending_asset(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    source = write_text_asset_source(repo, "brief.txt", "hello\n")
    _patch_surface_builder(monkeypatch, delay_seconds=0.05)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "add",
            os.fspath(source),
            "--path",
            os.fspath(repo),
            "--title",
            "Brief",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "comprehension: pending" in result.output
    assert "refresh" in result.output
    indexed = AssetIndex(paths).records()[0]
    assert indexed.comprehension_status == "pending"
    assert indexed.comprehension_current_version is None


def test_assets_add_requires_title_when_non_interactive(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    create_plan_run(repo)
    source = write_text_asset_source(repo, "brief.txt", "hello\n")

    result = runner.invoke(
        alysis_app,
        ["forge", "assets", "add", os.fspath(source), "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "--title is required when stdin is not a terminal" in result.output


def test_assets_add_dedupe_collision_requires_link(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    source = write_text_asset_source(repo, "brief.txt", "same\n")
    direct_surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    existing = direct_surface.add_asset(source, title="Brief", comprehend="skip").record
    _patch_surface_builder(monkeypatch)

    rejected = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "add",
            os.fspath(source),
            "--path",
            os.fspath(repo),
            "--title",
            "Again",
        ],
        env=_env(tmp_path),
    )
    linked = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "add",
            os.fspath(source),
            "--path",
            os.fspath(repo),
            "--title",
            "Again",
            "--link",
        ],
        env=_env(tmp_path),
    )

    assert rejected.exit_code == 2
    assert existing.id in rejected.output
    assert linked.exit_code == 0
    assert existing.id in linked.output
    assert AssetIndex(paths).get(existing.id).comprehension_status == "pending"


def test_assets_delete_yes_and_noninteractive_confirmation_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(repo, "brief.txt", "hello\n"),
        title="Brief",
        comprehend="skip",
    ).record
    _patch_surface_builder(monkeypatch)

    rejected = runner.invoke(
        alysis_app,
        ["forge", "assets", "delete", record.id, "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    deleted = runner.invoke(
        alysis_app,
        ["forge", "assets", "delete", record.id, "--path", os.fspath(repo), "--yes"],
        env=_env(tmp_path),
    )

    assert rejected.exit_code == 2
    assert "--yes is required" in rejected.output
    assert deleted.exit_code == 0
    assert "Deleted asset" in deleted.output


def test_assets_edit_and_refresh_wait(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(repo, "brief.txt", "hello\n"),
        title="Brief",
        comprehend="sync",
    ).record
    _patch_surface_builder(monkeypatch)

    edited = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "edit",
            record.id,
            "--path",
            os.fspath(repo),
            "--title",
            "Updated",
            "--pin",
        ],
        env=_env(tmp_path),
    )
    refreshed = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "refresh",
            record.id,
            "--path",
            os.fspath(repo),
            "--wait",
        ],
        env=_env(tmp_path),
    )

    assert edited.exit_code == 0
    assert "pinned: yes" in edited.output
    assert refreshed.exit_code == 0
    assert "comprehension: ready" in refreshed.output


def test_assets_edit_refresh_runs_synchronously(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(repo, "brief.txt", "hello\n"),
        title="Brief",
        comprehend="sync",
    ).record
    _patch_surface_builder(monkeypatch, delay_seconds=0.05)

    started = time.monotonic()
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "edit",
            record.id,
            "--path",
            os.fspath(repo),
            "--title",
            "Updated",
            "--refresh",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert time.monotonic() - started >= 0.04
    assert "comprehension: ready" in result.output
    assert AssetIndex(paths).get(record.id).comprehension_current_version == 2


def test_assets_refresh_without_wait_is_rejected(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(repo, "brief.txt", "hello\n"),
        title="Brief",
        comprehend="skip",
    ).record
    _patch_surface_builder(monkeypatch)

    result = runner.invoke(
        alysis_app,
        ["forge", "assets", "refresh", record.id, "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "CLI refresh requires --wait" in result.output


def test_assets_cancel_pending_reports_no_persistent_cli_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    create_plan_run(repo)
    _patch_surface_builder(monkeypatch)

    result = runner.invoke(
        alysis_app,
        ["forge", "assets", "cancel-pending", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "No persistent CLI background comprehensions" in result.output


def test_assets_error_paths(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    create_plan_run(repo)
    _patch_surface_builder(monkeypatch)

    missing = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "show",
            "ast_missing",
            "--path",
            os.fspath(repo),
            "--format",
            "json",
        ],
        env=_env(tmp_path),
    )
    bad_file = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "add",
            os.fspath(repo / "missing.txt"),
            "--path",
            os.fspath(repo),
            "--title",
            "Missing",
        ],
        env=_env(tmp_path),
    )
    bad_title = runner.invoke(
        alysis_app,
        [
            "forge",
            "assets",
            "add",
            os.fspath(write_text_asset_source(repo, "brief.txt", "hello\n")),
            "--path",
            os.fspath(repo),
            "--title",
            "",
        ],
        env=_env(tmp_path),
    )

    assert missing.exit_code == 2
    assert "Asset not found" in missing.output
    assert bad_file.exit_code == 2
    assert "does not exist" in bad_file.output
    assert bad_title.exit_code == 2
    assert "Asset title is required" in bad_title.output


def test_assets_check_plan_json_reports_drift(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(repo), title="Brief", comprehend="skip"
    ).record
    surface.delete_asset(record.id)
    plan = {
        "schema_version": 1,
        "run_id": paths.run_id,
        "created_at": "2026-05-03T00:00:00+00:00",
        "updated_at": "2026-05-03T00:00:00+00:00",
        "project_goal": "Goal",
        "summary": "Summary",
        "requirements": [],
        "tasks": [
            {
                "id": "T01",
                "title": "Task",
                "description": "Task",
                "acceptance_criteria": ["Done."],
                "dependencies": [],
                "estimated_files": [],
                "write_scope": [],
                "asset_briefing": {
                    "primary": [
                        {
                            "asset_id": record.id,
                            "rationale": "Was attached",
                            "expected_use": "Use it",
                        }
                    ],
                    "may_need": [
                        {
                            "asset_id": "ast_missing",
                            "rationale": "Missing",
                            "expected_use": "Use it",
                        }
                    ],
                },
            }
        ],
        "assets": [],
    }
    save_plan(paths, plan)
    _patch_surface_builder(monkeypatch)

    result = runner.invoke(
        alysis_app,
        ["forge", "assets", "check-plan", "--path", os.fspath(repo), "--format", "json"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["deleted_referenced"] == [{"asset_id": record.id, "task_id": "T01"}]
    assert payload["missing_referenced"] == [{"asset_id": "ast_missing", "task_id": "T01"}]
