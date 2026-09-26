from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qa.hash_vscode_release_evidence import (
    EXPECTED_FILENAMES,
    EvidenceInventoryError,
    evidence_inventory,
)


def _evidence(tmp_path: Path) -> Path:
    directory = tmp_path / "evidence"
    directory.mkdir(parents=True)
    for name in EXPECTED_FILENAMES:
        (directory / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    return directory


def test_inventory_digest_is_deterministic_and_binds_every_byte(tmp_path: Path) -> None:
    directory = _evidence(tmp_path)
    first = evidence_inventory(directory)
    second = evidence_inventory(directory)

    assert first == second
    assert len(first["files"]) == 9
    assert len(str(first["inventory_sha256"])) == 64

    target = directory / "vscode-alysis-linux-x64.manual-provider.json"
    target.write_text('{"changed":true}', encoding="utf-8")
    assert evidence_inventory(directory)["inventory_sha256"] != first["inventory_sha256"]


def test_inventory_rejects_missing_extra_and_symlink_entries(tmp_path: Path) -> None:
    directory = _evidence(tmp_path)
    (directory / next(iter(EXPECTED_FILENAMES))).unlink()
    with pytest.raises(EvidenceInventoryError, match="inventory mismatch"):
        evidence_inventory(directory)

    directory = _evidence(tmp_path / "extra")
    (directory / "unreviewed.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceInventoryError, match="inventory mismatch"):
        evidence_inventory(directory)

    directory = _evidence(tmp_path / "link")
    target = directory / next(iter(EXPECTED_FILENAMES))
    target.unlink()
    try:
        target.symlink_to(directory / next(iter(EXPECTED_FILENAMES - {target.name})))
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(EvidenceInventoryError):
        evidence_inventory(directory)
