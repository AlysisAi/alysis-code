from __future__ import annotations

from pathlib import Path
from typing import Any

from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import AssetSurface
from alysis_code.assets.models import AssetRecord
from alysis_code.assets.planner_tools import PlannerAssetToolRunner
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.model_registry import ModelRegistry


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="focused result",
            tool_calls=[],
            raw={},
        )


class _AngleComprehender(FakeAssetComprehender):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.angles: list[str | None] = []

    def comprehend(self, asset: AssetRecord, *, angle: str | None = None):
        self.angles.append(angle)
        return super().comprehend(asset, angle=angle)


def _surface(tmp_path: Path, comprehender: FakeAssetComprehender | None = None) -> AssetSurface:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    return AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=comprehender or FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )


def _runner(surface: AssetSurface) -> PlannerAssetToolRunner:
    return PlannerAssetToolRunner(
        cfg=surface.cfg,
        run_paths=surface.run_paths,
        surface=surface,
        model_registry=ModelRegistry(cfg=surface.cfg),
        api_key="k",
    )


def test_asset_read_returns_text_content(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "hello asset\n"),
        title="Asset",
        comprehend="sync",
    ).record

    result = _runner(surface).asset_read(asset_id=record.id)

    assert "hello asset" in result
    assert record.id in result


def test_asset_read_focus_uses_llm_and_cache(tmp_path: Path, monkeypatch) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "hello asset\n" * 500),
        title="Asset",
        comprehend="sync",
    ).record
    fake_client = _FakeClient()

    monkeypatch.setattr(
        "alysis_code.assets.planner_tools.make_llm_client",
        lambda **_kwargs: fake_client,
    )
    runner = _runner(surface)

    first = runner.asset_read(asset_id=record.id, focus="hello", max_chars=100)
    second = runner.asset_read(asset_id=record.id, focus="hello", max_chars=100)

    assert "focused result" in first
    assert "focused result" in second
    assert fake_client.calls == 1


def test_asset_read_deleted_asset_reports_deletion(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path), title="Asset", comprehend="skip"
    ).record
    surface.delete_asset(record.id)

    result = _runner(surface).asset_read(asset_id=record.id)

    assert "has been deleted" in result


def test_asset_inspect_refreshes_with_angle_and_enforces_cap(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    fake = _AngleComprehender(paths)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=fake,  # type: ignore[arg-type]
    )
    record = surface.add_asset(
        write_text_asset_source(tmp_path), title="Asset", comprehend="sync"
    ).record
    runner = _runner(surface)

    result = runner.asset_inspect(asset_id=record.id, angle="security")
    runner.asset_inspect(asset_id=record.id)
    runner.asset_inspect(asset_id=record.id)
    capped = runner.asset_inspect(asset_id=record.id)

    assert "Asset inspection complete" in result
    assert fake.angles[1] == "security"
    assert "Inspection cap reached" in capped
