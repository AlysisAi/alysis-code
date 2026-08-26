from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from PIL import Image

from alysis_code.assets import AssetAlreadyExistsError, AssetError, AssetIndex, ingest_asset
from alysis_code.forge import create_plan_run


def test_text_file_ingestion_sets_extracted_path_to_stored_path(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    source = tmp_path / "brief.txt"
    source.write_text("Γεια σου asset\n", encoding="utf-8")

    record = ingest_asset(
        source,
        title="Brief",
        description="",
        run_paths=paths,
        added_by={"phase": "test"},
    )

    assert record.kind == "text"
    assert record.extracted_text_path == record.stored_path
    assert record.thumbnail_path is None
    assert (paths.root / record.stored_path).read_text(encoding="utf-8") == "Γεια σου asset\n"


def test_image_ingestion_generates_thumbnail(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    source = tmp_path / "diagram.png"
    Image.new("RGB", (900, 300), color="white").save(source)

    record = ingest_asset(source, title="Diagram", run_paths=paths)

    assert record.kind == "image"
    assert record.extracted_text_path is None
    assert record.thumbnail_path is not None
    with Image.open(paths.root / record.thumbnail_path) as thumbnail:
        assert max(thumbnail.size) <= 512


def test_ingestion_rejects_unsupported_file_type_and_empty_title(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"\x00\x01")

    with pytest.raises(AssetError, match="Asset title is required"):
        ingest_asset(source, title="", run_paths=paths)
    with pytest.raises(AssetError, match="Unsupported asset file type"):
        ingest_asset(source, title="Binary", run_paths=paths)


def test_ingestion_computes_sha256(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    source = tmp_path / "brief.md"
    data = b"# Brief\n"
    source.write_bytes(data)

    record = ingest_asset(source, title="Brief", run_paths=paths)

    assert record.sha256 == hashlib.sha256(data).hexdigest()


def test_ingestion_dedupe_reject_and_link(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path)
    source = tmp_path / "brief.txt"
    source.write_text("same\n", encoding="utf-8")

    first = ingest_asset(source, title="Brief", run_paths=paths)
    with pytest.raises(AssetAlreadyExistsError) as exc_info:
        ingest_asset(source, title="Brief again", run_paths=paths)
    assert exc_info.value.existing_id == first.id

    linked = ingest_asset(source, title="Brief linked", run_paths=paths, dedupe_policy="link")
    assert linked.id == first.id
    assert len(AssetIndex(paths).records()) == 1


def test_concurrent_duplicate_ingestion_preserves_winner_raw_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = create_plan_run(tmp_path)
    source = tmp_path / "brief.txt"
    source.write_text("same\n", encoding="utf-8")
    barrier = threading.Barrier(2)
    original_find = AssetIndex.find_by_sha256

    def delayed_find_by_sha256(
        self: AssetIndex,
        sha256: str,
        *,
        include_deleted: bool = False,
    ):
        barrier.wait(timeout=5)
        return original_find(self, sha256, include_deleted=include_deleted)

    monkeypatch.setattr(AssetIndex, "find_by_sha256", delayed_find_by_sha256)
    records = []
    errors: list[Exception] = []

    def ingest() -> None:
        try:
            records.append(ingest_asset(source, title="Brief", run_paths=paths))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ingest) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(records) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], AssetAlreadyExistsError)
    indexed = AssetIndex(paths).records()
    assert len(indexed) == 1
    assert (paths.root / indexed[0].stored_path).read_text(encoding="utf-8") == "same\n"
