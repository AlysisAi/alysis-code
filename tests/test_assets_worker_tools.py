from __future__ import annotations

import json
from pathlib import Path

from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import AssetSurface
from alysis_code.assets.usage_logger import AssetUsageLogger
from alysis_code.assets.worker_mirror import mirror_task_assets
from alysis_code.assets.worker_tools import (
    build_worker_asset_mcp_manager,
    compose_worker_asset_mcp_manager,
)
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run
from alysis_code.mcp.manager import McpHostToolBinding
from alysis_code.model_registry import ModelRegistry


def _tool_case(tmp_path: Path):
    paths = create_plan_run(tmp_path, create_if_missing=True)
    cfg = AppConfig(model="fake-model")
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "local mirror content"),
        title="Asset",
        comprehend="sync",
    ).record
    workspace = tmp_path / "work"
    workspace.mkdir()
    task = {
        "id": "T01",
        "asset_briefing": {
            "primary": [
                {"asset_id": record.id, "rationale": "r", "expected_use": "u"},
            ],
            "may_need": [],
        },
    }
    mirror = mirror_task_assets(task=task, plan={}, surface=surface, workspace_path=workspace)
    logger = AssetUsageLogger(run_paths=paths, task_id="T01")
    manager = build_worker_asset_mcp_manager(
        cfg=cfg,
        surface=surface,
        model_registry=ModelRegistry(cfg=cfg),
        mirror=mirror,
        usage_logger=logger,
        api_key="k",
    )
    return record, paths, manager


def test_worker_asset_read_prefers_mirror_manifest(tmp_path: Path) -> None:
    record, _paths, manager = _tool_case(tmp_path)
    binding = {binding.tool_alias: binding for binding in manager.tool_bindings}["asset_read"]

    result = binding.run({"asset_id": record.id})

    assert "local mirror content" in result["text"]
    assert "[ASSET_UNTRUSTED_CONTENT]" in result["text"]
    assert result["cached"] is False


def test_worker_asset_load_returns_text_and_usage_event(tmp_path: Path) -> None:
    record, paths, manager = _tool_case(tmp_path)
    binding = {binding.tool_alias: binding for binding in manager.tool_bindings}["asset_load"]

    result = binding.run({"asset_id": record.id})

    assert result["available"] is True
    assert "local mirror content" in result["text"]
    assert "[ASSET_UNTRUSTED_CONTENT]" in result["text"]
    events = [
        json.loads(line)
        for line in (paths.execution_asset_usage_dir / "T01.jsonl").read_text("utf-8").splitlines()
    ]
    assert events[-1]["event"] == "asset_load"
    assert events[-1]["asset_id"] == record.id


def test_worker_asset_load_deleted_asset_is_clear(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    cfg = AppConfig(model="fake-model")
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "content"),
        title="Asset",
        comprehend="sync",
    ).record
    surface.delete_asset(record.id)
    workspace = tmp_path / "work"
    workspace.mkdir()
    task = {
        "id": "T01",
        "asset_briefing": {
            "primary": [
                {"asset_id": record.id, "rationale": "r", "expected_use": "u"},
            ],
            "may_need": [],
        },
    }
    mirror = mirror_task_assets(task=task, plan={}, surface=surface, workspace_path=workspace)
    manager = build_worker_asset_mcp_manager(
        cfg=cfg,
        surface=surface,
        model_registry=ModelRegistry(cfg=cfg),
        mirror=mirror,
    )
    binding = {binding.tool_alias: binding for binding in manager.tool_bindings}["asset_load"]

    result = binding.run({"asset_id": record.id})

    assert result["available"] is False
    assert "deleted" in result["text"]


def test_worker_asset_load_deleted_asset_writes_usage_event(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    cfg = AppConfig(model="fake-model")
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "content"),
        title="Asset",
        comprehend="sync",
    ).record
    surface.delete_asset(record.id)
    workspace = tmp_path / "work"
    workspace.mkdir()
    task = {
        "id": "T01",
        "asset_briefing": {
            "primary": [
                {"asset_id": record.id, "rationale": "r", "expected_use": "u"},
            ],
            "may_need": [],
        },
    }
    mirror = mirror_task_assets(task=task, plan={}, surface=surface, workspace_path=workspace)
    logger = AssetUsageLogger(run_paths=paths, task_id="T01")
    manager = build_worker_asset_mcp_manager(
        cfg=cfg,
        surface=surface,
        model_registry=ModelRegistry(cfg=cfg),
        mirror=mirror,
        usage_logger=logger,
    )
    binding = {binding.tool_alias: binding for binding in manager.tool_bindings}["asset_load"]

    result = binding.run({"asset_id": record.id})

    assert result["available"] is False
    events = [
        json.loads(line)
        for line in (paths.execution_asset_usage_dir / "T01.jsonl").read_text("utf-8").splitlines()
    ]
    assert events[-1]["event"] == "asset_load"
    assert events[-1]["asset_id"] == record.id


def test_composite_worker_asset_mcp_manager_preserves_base_tools(tmp_path: Path) -> None:
    record, _paths, asset_manager = _tool_case(tmp_path)
    _ = record
    base_binding = McpHostToolBinding(
        tool_name="echo",
        tool_alias="echo",
        description="Echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run_handler=lambda _args: {"ok": True},
    )

    class _BaseManager:
        resolved_config = type("_Resolved", (), {"has_any_config": True})()
        closed = False

        @property
        def tool_bindings(self):  # type: ignore[no-untyped-def]
            return (base_binding,)

        def startup_metadata(self):  # type: ignore[no-untyped-def]
            return {"config_present": True}

        def catalog_snapshot_metadata(self):  # type: ignore[no-untyped-def]
            return {
                "exposed_tool_aliases": ["echo"],
                "exposed_tool_names": ["echo"],
                "exposed_tool_count": 1,
            }

        def execution_context_summary(self):  # type: ignore[no-untyped-def]
            return {"servers": [{"id": "base"}]}

        def close(self) -> None:
            self.closed = True

    base = _BaseManager()
    composite = compose_worker_asset_mcp_manager(
        base_manager=base,
        asset_manager=asset_manager,
    )

    aliases = [binding.tool_alias for binding in composite.tool_bindings]

    assert aliases == ["echo", "asset_read", "asset_load"]
    assert composite.catalog_snapshot_metadata()["exposed_tool_aliases"] == [
        "echo",
        "asset_read",
        "asset_load",
    ]
