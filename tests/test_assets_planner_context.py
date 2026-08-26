from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source
from PIL import Image

from alysis_code.assets import (
    AssetError,
    AssetReadinessPolicy,
    AssetSurface,
    asset_reference_check,
    build_planner_assets_block,
    ensure_planner_asset_readiness,
)
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run


def _surface(tmp_path: Path, *, delay_seconds: float = 0.0) -> AssetSurface:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    return AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths, delay_seconds=delay_seconds),  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 5, 3, tzinfo=UTC),
    )


def test_empty_planner_assets_block_is_explicit(tmp_path: Path) -> None:
    surface = _surface(tmp_path)

    context = build_planner_assets_block(surface)

    assert "## Available Assets" in context.text_block
    assert "- (no assets attached)" in context.text_block


def test_planner_assets_block_renders_mixed_states_and_preserves_titles(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    ready = surface.add_asset(
        write_text_asset_source(tmp_path, "ready.txt", "ready\n"),
        title="Σύντομο spec",
        pinned=True,
        comprehend="sync",
    ).record
    pending = surface.add_asset(
        write_text_asset_source(tmp_path, "pending.txt", "pending\n"),
        title="Pending",
        comprehend="skip",
    ).record
    minimal = surface.add_asset(
        write_text_asset_source(tmp_path, "minimal.txt", "minimal\n"),
        title="Minimal",
        description="Only metadata",
        comprehend="skip",
    ).record
    failed = surface.add_asset(
        write_text_asset_source(tmp_path, "failed.txt", "failed\n"),
        title="Failed",
        comprehend="skip",
    ).record
    surface.index.update(replace(minimal, comprehension_status="minimal"))
    surface.index.update(replace(failed, comprehension_status="failed"))

    context = build_planner_assets_block(surface)
    partial = build_planner_assets_block(surface, include_pending=True)

    assert context.text_block.index(ready.id) < context.text_block.index(minimal.id)
    assert "### " + ready.id + ' - "Σύντομο spec"' in context.text_block
    assert pending.id not in context.text_block
    assert "minimal comprehension" in context.text_block
    assert "comprehension failed" in context.text_block
    assert pending.id in context.pending_asset_ids
    assert pending.id in partial.text_block
    assert "comprehension pending" in partial.text_block


def test_planner_assets_block_warns_when_image_is_minimal(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    source = tmp_path / "screenshot.png"
    Image.new("RGB", (20, 20), color="white").save(source)
    record = surface.add_asset(
        source,
        title="Screenshot",
        description="Only a user description exists.",
        comprehend="skip",
    ).record
    surface.index.update(replace(record, comprehension_status="minimal"))

    context = build_planner_assets_block(surface)

    assert "minimal comprehension" in context.text_block
    assert "no structured visual or OCR understanding is available" in context.text_block


def test_planner_assets_block_truncates_per_asset(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    cfg = AppConfig(model="fake-model")
    cfg.assets.planner.max_chars_per_asset = 120
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths, summary_prefix="x" * 500),  # type: ignore[arg-type]
    )
    surface.add_asset(write_text_asset_source(tmp_path), title="Long", comprehend="sync")

    context = build_planner_assets_block(surface)

    assert "... (truncated for context budget)" in context.text_block


def test_readiness_policies_block_soft_and_timeout(tmp_path: Path) -> None:
    surface = _surface(tmp_path, delay_seconds=0.05)
    record = surface.add_asset(
        write_text_asset_source(tmp_path), title="A", comprehend="skip"
    ).record
    surface.refresh_comprehension(record.id, mode="async")

    soft = ensure_planner_asset_readiness(surface, policy=AssetReadinessPolicy.SOFT)
    assert record.id in soft.pending

    block = ensure_planner_asset_readiness(
        surface,
        policy=AssetReadinessPolicy.BLOCK,
        timeout_seconds=2,
    )
    assert record.id in block.ready

    slow_root = tmp_path / "slow"
    slow_root.mkdir()
    slow = _surface(slow_root, delay_seconds=0.3)
    slow_record = slow.add_asset(
        write_text_asset_source(slow_root, "slow.txt", "slow\n"),
        title="Slow",
        comprehend="skip",
    ).record
    handle = slow.refresh_comprehension(slow_record.id, mode="async")
    with pytest.raises(AssetError, match="did not finish"):
        ensure_planner_asset_readiness(
            slow,
            policy=AssetReadinessPolicy.BLOCK,
            timeout_seconds=0.01,
        )
    handle.join(timeout=2)


def test_block_readiness_starts_pending_asset_without_existing_thread(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "needs-comprehension.txt", "content\n"),
        title="Needs comprehension",
        comprehend="skip",
    ).record

    report = ensure_planner_asset_readiness(
        surface,
        policy=AssetReadinessPolicy.BLOCK,
        timeout_seconds=2,
    )

    assert record.id in report.ready
    assert surface.index.get(record.id).comprehension_status == "ready"


def test_soft_readiness_starts_pending_asset_without_blocking(tmp_path: Path) -> None:
    surface = _surface(tmp_path, delay_seconds=0.05)
    record = surface.add_asset(
        write_text_asset_source(tmp_path, "soft.txt", "content\n"),
        title="Soft",
        comprehend="skip",
    ).record

    report = ensure_planner_asset_readiness(surface, policy=AssetReadinessPolicy.SOFT)

    assert record.id in report.pending
    surface.join_pending(timeout_seconds=2)
    assert surface.index.get(record.id).comprehension_status == "ready"


def test_asset_reference_check_reports_deleted_missing_and_unbound_pinned(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    referenced = surface.add_asset(
        write_text_asset_source(tmp_path, "ref.txt", "ref\n"),
        title="Referenced",
        comprehend="skip",
    ).record
    pinned = surface.add_asset(
        write_text_asset_source(tmp_path, "pin.txt", "pin\n"),
        title="Pinned",
        pinned=True,
        comprehend="skip",
    ).record
    surface.delete_asset(referenced.id)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "asset_briefing": {
                    "primary": [
                        {
                            "asset_id": referenced.id,
                            "rationale": "Was useful",
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
        ]
    }

    report = asset_reference_check(plan, surface)

    assert report.deleted_referenced == [("T01", referenced.id)]
    assert report.missing_referenced == [("T01", "ast_missing")]
    assert report.pinned_added == [pinned.id]


def test_asset_reference_check_tolerates_malformed_briefing(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    pinned = surface.add_asset(
        write_text_asset_source(tmp_path, "pin.txt", "pin\n"),
        title="Pinned",
        pinned=True,
        comprehend="skip",
    ).record
    plan = {
        "tasks": [
            {
                "id": "T01",
                "asset_briefing": {"primary": {"asset_id": pinned.id}},
            }
        ]
    }

    report = asset_reference_check(plan, surface)

    assert report.deleted_referenced == []
    assert report.missing_referenced == []
    assert report.pinned_added == [pinned.id]
