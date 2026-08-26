from __future__ import annotations

import json
import os
import re
from pathlib import Path

from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.assets import AssetSurface
from alysis_code.assets.budget_allocator import (
    AssetInclusionDecision,
    TaskAssetAllocation,
)
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl import forge as forge_cli_impl
from alysis_code.cli_impl.commands.forge_helpers import _show_forge_plan_summary
from alysis_code.config import AppConfig
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _asset_cfg() -> AppConfig:
    return AppConfig(model="deepseek-v4-pro")


def _add_legacy_plan_asset(paths, plan: dict, *, name: str, text: str) -> None:  # type: ignore[no-untyped-def]
    asset_path = paths.assets_dir / name
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_text(text, encoding="utf-8")
    plan.setdefault("assets", []).append(
        {
            "original_path": name,
            "stored_path": asset_path.relative_to(paths.root).as_posix(),
            "size_bytes": asset_path.stat().st_size,
            "added_at": "2026-05-20T00:00:00+00:00",
        }
    )


def test_forge_exec_injects_task_assets_and_asset_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    cfg = _asset_cfg()
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    asset = surface.add_asset(
        write_text_asset_source(tmp_path, "spec.txt", "asset guidance for implemented.txt"),
        title="Implementation Spec",
        comprehend="sync",
    ).record
    plan = load_plan(paths)
    plan["project_goal"] = "Execute with assets"
    plan["summary"] = "Execute with assets"
    task = add_task(
        plan,
        title="Implement asset-backed file",
        description="Use the attached implementation spec.",
        estimated_files=["implemented.txt"],
        branch="feat/t01-assets",
    )
    task["write_scope"] = ["implemented.txt"]
    task["asset_briefing"] = {
        "primary": [
            {
                "asset_id": asset.id,
                "rationale": "Contains implementation details.",
                "expected_use": "Use it to write implemented.txt.",
            }
        ],
        "may_need": [],
    }
    save_plan(paths, plan)
    captured: dict[str, object] = {}

    def fake_allocate(**kwargs):  # type: ignore[no-untyped-def]
        mirror = kwargs["mirror"]
        assert mirror.primary
        return TaskAssetAllocation(
            task_id=str(task["id"]),
            decisions=[
                AssetInclusionDecision(
                    asset_id=mirror.primary[0].asset_id,
                    mode="full_inline",
                    focus=None,
                    reason="small primary text asset",
                )
            ],
            elapsed_ms=1,
            model=None,
            tokens_used={},
            fallback_used=False,
            fallback_reason=None,
        )

    def fake_run_agent(*, root: Path, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["instruction"] = kwargs["instruction"]
        captured["tool_aliases"] = [
            binding.tool_alias for binding in kwargs["mcp_manager"].tool_bindings
        ]
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(forge_cli_impl, "allocate_task_assets", fake_allocate)
    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            str(task["id"]),
            "--path",
            os.fspath(repo),
            "--model",
            "deepseek-v4-pro",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    instruction = str(captured["instruction"])
    assert "## Relevant Assets" in instruction
    assert asset.id in instruction
    assert "asset guidance for implemented.txt" in instruction
    assert {"asset_read", "asset_load"}.issubset(set(captured["tool_aliases"]))  # type: ignore[arg-type]
    assert (repo / ".alysis" / "task_assets" / "manifest.json").exists()
    allocation_payload = json.loads(
        (paths.execution_asset_briefings_dir / f"{task['id']}.json").read_text("utf-8")
    )
    assert allocation_payload["attempts"][0]["decisions"][0]["asset_id"] == asset.id


def test_forge_summary_and_status_count_indexed_assets_when_plan_assets_empty(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    cfg = _asset_cfg()
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    surface.add_asset(
        write_text_asset_source(tmp_path, "notes.txt", "asset notes"),
        title="Notes",
        comprehend="sync",
    )
    plan = load_plan(paths)
    assert plan.get("assets") == []

    console = Console(record=True, force_terminal=False, width=120)
    _show_forge_plan_summary(console=console, paths=paths, plan=plan)
    summary_output = console.export_text()
    assert f"Run {paths.run_id} · 0 tasks · 1 assets" in summary_output
    assert "Assets · 1" in summary_output

    status_result = runner.invoke(
        alysis_app,
        ["forge", "status", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert status_result.exit_code == 0, status_result.output
    assert re.search(r"assets\s+│\s+1", status_result.output)


def test_forge_summary_status_and_show_count_mixed_indexed_and_legacy_assets(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    cfg = _asset_cfg()
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    surface.add_asset(
        write_text_asset_source(tmp_path, "indexed.txt", "indexed notes"),
        title="Indexed Notes",
        comprehend="sync",
    )
    plan = load_plan(paths)
    _add_legacy_plan_asset(paths, plan, name="legacy.txt", text="legacy notes")
    save_plan(paths, plan)

    console = Console(record=True, force_terminal=False, width=120)
    _show_forge_plan_summary(console=console, paths=paths, plan=load_plan(paths))
    summary_output = console.export_text()
    assert f"Run {paths.run_id} · 0 tasks · 2 assets" in summary_output
    assert "Assets · 2" in summary_output

    status_result = runner.invoke(
        alysis_app,
        ["forge", "status", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert status_result.exit_code == 0, status_result.output
    assert re.search(r"assets\s+│\s+2", status_result.output)

    show_result = runner.invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert show_result.exit_code == 0, show_result.output
    assert "indexed" in show_result.output
    assert "legacy" in show_result.output
    assert "indexed.txt" in show_result.output
    assert "legacy.txt" in show_result.output
