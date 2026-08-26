from __future__ import annotations

import pytest

from alysis_code.assets import (
    AssetAlreadyExistsError,
    AssetError,
    AssetRecord,
    ComprehensionData,
    ComprehensionRecord,
)


def _asset_payload() -> dict:
    return {
        "id": "ast_1234abcd",
        "title": "Σχέδιο",
        "description": "Περιγραφή",
        "kind": "text",
        "mime": "text/plain",
        "original_filename": "brief.txt",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "stored_path": ".alysis/runs/r/assets/raw/ast_1234abcd/brief.txt",
        "extracted_text_path": ".alysis/runs/r/assets/raw/ast_1234abcd/brief.txt",
        "thumbnail_path": None,
        "pinned": False,
        "added_at": "2026-05-03T00:00:00+00:00",
        "added_by": {"phase": "test"},
        "deleted_at": None,
        "comprehension_status": "pending",
        "comprehension_current_version": None,
    }


def test_asset_record_round_trips_non_latin_values() -> None:
    record = AssetRecord.from_dict(_asset_payload())

    assert record.title == "Σχέδιο"
    assert record.to_dict()["description"] == "Περιγραφή"


def test_asset_record_rejects_invalid_literals() -> None:
    payload = _asset_payload()
    payload["kind"] = "pdf"

    with pytest.raises(AssetError, match="kind must be one of"):
        AssetRecord.from_dict(payload)


def test_comprehension_record_round_trips_defaults() -> None:
    record = ComprehensionRecord(
        schema_version=1,
        version=1,
        asset_id="ast_1234abcd",
        status="ready",
        source="text_only",
        model="model",
        role="comprehension",
        ocr_engine=None,
        ocr_languages_used=[],
        detected_language="el",
        language_confidence=0.91,
        confidence_modifier=1.0,
        tokens_used={"input": 10, "output": 20},
        elapsed_ms=5,
        generated_at="2026-05-03T00:00:00+00:00",
        error=None,
        data=ComprehensionData(
            semantic_summary="Περίληψη",
            key_entities=[{"type": "endpoint", "value": "/auth/login"}],
        ),
    )

    loaded = ComprehensionRecord.from_dict(record.to_dict())

    assert loaded.data.semantic_summary == "Περίληψη"
    assert loaded.tokens_used == {"input": 10, "output": 20}


def test_asset_already_exists_error_carries_existing_id() -> None:
    error = AssetAlreadyExistsError("ast_1234abcd")

    assert error.existing_id == "ast_1234abcd"
    assert "ast_1234abcd" in str(error)
