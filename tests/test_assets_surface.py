from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import (
    AssetAlreadyExistsError,
    AssetIndex,
    AssetSurface,
)
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run


def _surface(
    tmp_path: Path,
    *,
    delay_seconds: float = 0.0,
) -> tuple[AssetSurface, FakeAssetComprehender]:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    fake = FakeAssetComprehender(paths, delay_seconds=delay_seconds)
    surface = AssetSurface(
        cfg=AppConfig(model="fake-model"),
        run_paths=paths,
        comprehender=fake,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 5, 3, tzinfo=UTC),
    )
    return surface, fake


def _wait_for_comprehension(surface: AssetSurface, asset_id: str):
    for _ in range(100):
        record = surface.comprehension_for(asset_id)
        if record is not None:
            return record
        time.sleep(0.01)
    raise AssertionError("comprehension did not finish")


def test_add_asset_sync_returns_attached_comprehension(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)

    result = surface.add_asset(source, title="Brief", comprehend="sync")

    assert result.comprehension_handle is None
    assert result.comprehension_record is not None
    assert result.comprehension_record.version == 1
    assert surface.comprehension_for(result.record.id).data.semantic_summary == "Summary 1"


def test_add_asset_async_reports_running_then_ready(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path, delay_seconds=0.05)
    source = write_text_asset_source(tmp_path)

    result = surface.add_asset(source, title="Brief", comprehend="async")

    assert result.comprehension_handle is not None
    assert surface.list_assets()[0].comprehension_status == "running"
    record = result.comprehension_handle.join(timeout=2)
    assert record is not None
    assert record.status == "ready"
    assert surface.list_assets()[0].comprehension_status == "ready"


def test_add_asset_skip_leaves_pending_without_thread(tmp_path: Path) -> None:
    surface, fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)

    result = surface.add_asset(source, title="Brief", comprehend="skip")

    assert result.comprehension_handle is None
    assert result.comprehension_record is None
    assert fake.calls == []
    assert surface.list_assets()[0].comprehension_status == "pending"


def test_identity_collision_rejects_or_links_existing_record(tmp_path: Path) -> None:
    surface, fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)
    first = surface.add_asset(source, title="Brief", comprehend="skip").record

    with pytest.raises(AssetAlreadyExistsError) as exc_info:
        surface.add_asset(source, title="Again", comprehend="skip")
    assert exc_info.value.existing_id == first.id

    linked = surface.add_asset(
        source,
        title="Again",
        comprehend="async",
        dedupe_policy="link",
    )
    assert linked.record.id == first.id
    assert linked.comprehension_handle is None
    assert linked.comprehension_record is None
    assert fake.calls == []


def test_show_asset_reports_transient_running_status(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path, delay_seconds=0.05)
    source = write_text_asset_source(tmp_path)
    result = surface.add_asset(source, title="Brief", comprehend="skip")

    handle = surface.refresh_comprehension(result.record.id, mode="async")

    assert surface.show_asset(result.record.id).comprehension_status == "running"
    assert handle.join(timeout=2) is not None


def test_edit_asset_updates_metadata_without_refreshing_by_default(tmp_path: Path) -> None:
    surface, fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)
    result = surface.add_asset(source, title="Brief", comprehend="sync")

    detail = surface.edit_asset(
        result.record.id,
        title="Updated",
        description="Νέα περιγραφή",
        pinned=True,
    )

    assert detail.record.title == "Updated"
    assert detail.record.description == "Νέα περιγραφή"
    assert detail.record.pinned is True
    assert fake.calls == [result.record.id]
    assert surface.comprehension_for(result.record.id).version == 1


def test_edit_asset_can_retrigger_async_refresh(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)
    result = surface.add_asset(source, title="Brief", comprehend="sync")

    surface.edit_asset(result.record.id, title="Updated", retrigger_comprehension=True)
    record = _wait_for_comprehension(surface, result.record.id)

    assert record.version == 2
    assert record.data.semantic_summary == "Summary 2"


def test_delete_tombstones_record_and_default_list_excludes_it(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)
    result = surface.add_asset(source, title="Brief", comprehend="skip")

    deleted = surface.delete_asset(result.record.id)

    assert deleted.deleted_at is not None
    assert surface.list_assets() == []
    assert surface.list_assets(include_deleted=True)[0].record.deleted_at is not None


def test_refresh_comprehension_sync_returns_new_version(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)
    result = surface.add_asset(source, title="Brief", comprehend="sync")

    handle = surface.refresh_comprehension(result.record.id, mode="sync")

    record = handle.join()
    assert record is not None
    assert record.version == 2


def test_concurrent_refresh_cancels_prior_callback(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path, delay_seconds=0.05)
    source = write_text_asset_source(tmp_path)
    result = surface.add_asset(source, title="Brief", comprehend="skip")
    callbacks: list[int] = []

    first = surface.refresh_comprehension(
        result.record.id,
        mode="async",
        callback=lambda record: callbacks.append(record.version),
    )
    second = surface.refresh_comprehension(
        result.record.id,
        mode="async",
        callback=lambda record: callbacks.append(record.version),
    )

    assert first.cancellation_event.is_set()
    record = second.join(timeout=2)
    assert record is not None
    assert callbacks == [2]


def test_cancel_pending_comprehensions_signals_all_running_handles(tmp_path: Path) -> None:
    surface, _fake = _surface(tmp_path, delay_seconds=0.1)
    first = surface.add_asset(
        write_text_asset_source(tmp_path, "a.txt", "a\n"),
        title="A",
        comprehend="skip",
    ).record
    second = surface.add_asset(
        write_text_asset_source(tmp_path, "b.txt", "b\n"),
        title="B",
        comprehend="skip",
    ).record
    first_handle = surface.refresh_comprehension(first.id, mode="async")
    second_handle = surface.refresh_comprehension(second.id, mode="async")

    count = surface.cancel_pending_comprehensions()

    assert count == 2
    assert first_handle.cancellation_event.is_set()
    assert second_handle.cancellation_event.is_set()
    first_handle.join(timeout=2)
    second_handle.join(timeout=2)


def test_async_callback_failure_does_not_corrupt_index(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    surface, _fake = _surface(tmp_path)
    source = write_text_asset_source(tmp_path)
    result = surface.add_asset(source, title="Brief", comprehend="skip")

    def failing_callback(_record) -> None:
        raise RuntimeError("callback failed")

    with caplog.at_level(logging.WARNING):
        handle = surface.refresh_comprehension(
            result.record.id,
            mode="async",
            callback=failing_callback,
        )
        handle.join(timeout=2)

    indexed = AssetIndex(surface.run_paths).get(result.record.id)
    assert indexed.comprehension_status == "ready"
    assert "callback failed" in caplog.text
