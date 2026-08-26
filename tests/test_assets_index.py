from __future__ import annotations

import json
import threading
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code.assets import AssetError, AssetIndex, AssetRecord, ComprehensionData
from alysis_code.assets.index import (
    _AssetIndexLock,
    _build_lock_metadata,
    _metadata_text,
)
from alysis_code.assets.models import ComprehensionRecord
from alysis_code.atomic_io import atomic_write_json
from alysis_code.forge import create_plan_run


def _record(asset_id: str, sha: str, *, added_at: str = "2026-05-03T00:00:00+00:00") -> AssetRecord:
    return AssetRecord(
        id=asset_id,
        title=f"Asset {asset_id}",
        description="",
        kind="text",
        mime="text/plain",
        original_filename=f"{asset_id}.txt",
        size_bytes=5,
        sha256=sha,
        stored_path=f".alysis/runs/r/assets/raw/{asset_id}/{asset_id}.txt",
        extracted_text_path=f".alysis/runs/r/assets/raw/{asset_id}/{asset_id}.txt",
        thumbnail_path=None,
        pinned=False,
        added_at=added_at,
        added_by={"phase": "test"},
        deleted_at=None,
        comprehension_status="pending",
        comprehension_current_version=None,
    )


def _comprehension(asset_id: str, summary: str, *, entity: str = "/a") -> ComprehensionRecord:
    return ComprehensionRecord(
        schema_version=1,
        version=0,
        asset_id=asset_id,
        status="ready",
        source="text_only",
        model="model",
        role="comprehension",
        ocr_engine=None,
        ocr_languages_used=[],
        detected_language="en",
        language_confidence=0.9,
        confidence_modifier=1.0,
        tokens_used={},
        elapsed_ms=1,
        generated_at="2026-05-03T00:00:00+00:00",
        error=None,
        data=ComprehensionData(
            semantic_summary=summary,
            key_entities=[{"type": "endpoint", "value": entity}],
            stated_facts=[summary],
        ),
    )


def test_index_atomic_add_update_delete_and_dedupe(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    index = AssetIndex(paths)
    first = _record("ast_aaaaaaaa", "a" * 64)

    added = index.add(first)
    assert added.id == first.id
    assert index.load()["by_sha256"][first.sha256] == first.id

    updated = index.update(replace(first, pinned=True))
    assert updated.pinned is True
    assert index.get(first.id).pinned is True

    deleted = index.delete(first.id)
    assert deleted.deleted_at is not None
    assert index.records() == []
    assert index.get(first.id, include_deleted=True).deleted_at is not None
    assert first.sha256 not in index.load()["by_sha256"]


def test_index_concurrent_adds_keep_json_valid(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    index = AssetIndex(paths)
    errors: list[str] = []

    def add_record(batch: int, i: int) -> None:
        try:
            ordinal = (batch * 8) + i
            index.add(
                _record(
                    f"ast_{ordinal:08x}",
                    f"{ordinal:064x}"[-64:],
                    added_at=f"2026-05-03T00:{batch:02d}:{i:02d}+00:00",
                )
            )
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())

    for batch in range(20):
        threads = [threading.Thread(target=add_record, args=(batch, i)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert errors == []
    payload = json.loads(paths.assets_index_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert len(payload["assets"]) == 160
    assert not paths.assets_index_lock_path.exists()


def test_lock_release_retries_windows_reader_delete_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = create_plan_run(tmp_path)
    lock = _AssetIndexLock(paths.assets_index_lock_path)
    lock.__enter__()
    real_unlink = Path.unlink
    attempts = 0

    def flaky_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal attempts
        if path == paths.assets_index_lock_path and attempts < 3:
            attempts += 1
            raise PermissionError(13, "lock file is temporarily open by a reader")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    lock.release()

    assert attempts == 3
    assert not paths.assets_index_lock_path.exists()


def test_add_with_publish_rolls_back_partial_artifacts(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    index = AssetIndex(paths)
    record = _record("ast_aaaaaaaa", "a" * 64)
    artifact = paths.assets_raw_dir / record.id / record.original_filename

    def publish(_record: AssetRecord) -> None:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("partial", encoding="utf-8")
        raise AssetError("publish failed")

    def rollback(_record: AssetRecord) -> None:
        artifact.unlink()

    with pytest.raises(AssetError, match="publish failed"):
        index.add_with_publish(
            sha256=record.sha256,
            dedupe_policy="reject",
            build_record=lambda _existing_ids: record,
            publish=publish,
            rollback=rollback,
        )

    assert index.records() == []
    assert not artifact.exists()


def test_stale_lock_recovery_does_not_remove_replaced_fresh_lock(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    lock = _AssetIndexLock(paths.assets_index_lock_path)
    stale = _build_lock_metadata(owner_token="stale", kind="lock")
    fresh = _build_lock_metadata(owner_token="fresh", kind="lock")
    stale_text = _metadata_text(stale)

    paths.assets_index_lock_path.write_text(_metadata_text(fresh), encoding="utf-8")

    recovered = lock._recover_stale_lock(
        existing_metadata=stale,
        existing_text=stale_text,
        replacement_text=_metadata_text(
            _build_lock_metadata(owner_token=lock.owner_token, kind="lock")
        ),
    )

    assert recovered is False
    assert (
        json.loads(paths.assets_index_lock_path.read_text(encoding="utf-8"))["owner_token"]
        == "fresh"
    )


def test_stale_lock_recovery_replaces_only_verified_stale_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = create_plan_run(tmp_path)
    lock = _AssetIndexLock(paths.assets_index_lock_path)
    stale = _build_lock_metadata(owner_token="stale", kind="lock")
    stale_text = _metadata_text(stale)
    replacement_text = _metadata_text(
        _build_lock_metadata(owner_token=lock.owner_token, kind="lock")
    )
    paths.assets_index_lock_path.write_text(stale_text, encoding="utf-8")
    monkeypatch.setattr(
        "alysis_code.assets.index._lock_metadata_is_stale",
        lambda metadata: metadata.get("owner_token") == "stale",
    )

    recovered = lock._recover_stale_lock(
        existing_metadata=stale,
        existing_text=stale_text,
        replacement_text=replacement_text,
    )

    assert recovered is True
    assert (
        json.loads(paths.assets_index_lock_path.read_text(encoding="utf-8"))["owner_token"]
        == lock.owner_token
    )
    lock.release()
    assert not paths.assets_index_lock_path.exists()


def test_index_v1_schema_raises_migration_error(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    paths.assets_index_path.write_text('{"schema_version": 1, "assets": []}\n', encoding="utf-8")

    with pytest.raises(AssetError, match="legacy asset migration"):
        AssetIndex(paths).load()


def test_diff_comprehension_reports_expected_changes(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    index = AssetIndex(paths)
    asset = index.add(_record("ast_aaaaaaaa", "a" * 64))

    v1 = index.write_comprehension(_comprehension(asset.id, "Old", entity="/old"))
    v2 = index.write_comprehension(_comprehension(asset.id, "New", entity="/new"))

    diff = index.diff_comprehension(asset.id, v1.version, v2.version)
    assert diff["changed_fields"]["semantic_summary"] == {"from": "Old", "to": "New"}
    assert diff["key_entities"]["added"] == [{"type": "endpoint", "value": "/new"}]
    assert diff["key_entities"]["removed"] == [{"type": "endpoint", "value": "/old"}]
    assert index.read_comprehension(asset.id).version == 2


def test_comprehension_recovery_uses_existing_versions_when_index_or_pointer_lags(
    tmp_path: Path,
) -> None:
    paths = create_plan_run(tmp_path)
    index = AssetIndex(paths)
    asset = index.add(_record("ast_aaaaaaaa", "a" * 64))
    v1 = replace(_comprehension(asset.id, "Recovered"), version=1)
    asset_dir = paths.assets_comprehensions_dir / asset.id
    atomic_write_json(asset_dir / "v1.json", v1.to_dict())

    assert index.read_comprehension(asset.id).version == 1
    cached = index.find_existing_comprehension_by_sha256(
        asset.sha256,
        model="model",
        role="comprehension",
    )
    assert cached is not None
    assert cached.version == 1

    v2 = index.write_comprehension(_comprehension(asset.id, "Next"))

    assert v2.version == 2
    assert index.get(asset.id).comprehension_current_version == 2
    pointer = json.loads((asset_dir / "current.json").read_text(encoding="utf-8"))
    assert pointer["version"] == 2
    assert (asset_dir / "v1.json").exists()
    atomic_write_json(
        asset_dir / "current.json",
        {"asset_id": asset.id, "updated_at": "2026-05-03T00:00:00+00:00", "version": 1},
    )
    assert index.read_comprehension(asset.id).version == 2
