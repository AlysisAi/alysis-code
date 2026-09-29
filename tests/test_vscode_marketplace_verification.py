from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.qa.vscode_extension_dogfood import SUPPORTED_PLATFORM_TARGETS
from scripts.release.build_vscode_marketplace_verification import (
    MarketplaceVerificationError,
    build_receipt,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, Path]:
    candidates = tmp_path / "candidates"
    published = tmp_path / "published"
    candidates.mkdir()
    published.mkdir()
    candidate_records: dict[str, dict[str, str]] = {}
    existing: dict[str, dict[str, str]] = {}
    for target in sorted(SUPPORTED_PLATFORM_TARGETS):
        filename = f"vscode-alysis-{target}.vsix"
        content = f"candidate:{target}".encode()
        (candidates / filename).write_bytes(content)
        (published / filename).write_bytes(content)
        digest = _sha(content)
        candidate_records[target] = {"filename": filename, "sha256": digest}
        existing[target] = {
            "candidate": filename,
            "download_url": f"https://marketplace.visualstudio.com/{target}.vsix",
            "sha256": digest,
        }
    before_plan = {
        "schema_name": "vscode-marketplace-publication-plan",
        "schema_version": 2,
        "channel": "stable",
        "extension_id": "alysisai.vscode-alysis",
        "version": "1.2.3",
        "candidates": candidate_records,
        "existing": {},
        "missing": [
            {"target": target, "candidate": candidate_records[target]["filename"]}
            for target in sorted(SUPPORTED_PLATFORM_TARGETS)
        ],
    }
    observed_plan = {**before_plan, "existing": existing, "missing": []}
    paths = {"candidates": candidates, "published": published}
    for name, value in (
        ("before_query", {"state": "before"}),
        ("after_query", {"state": "after"}),
        ("before_plan", before_plan),
        ("observed_plan", observed_plan),
        ("validation_receipt", {"receipt": "bound"}),
        ("approval_binding", {"approval": "bound"}),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths[name] = path
    return paths


def _build(paths: dict[str, Path], *, channel: str = "stable") -> dict[str, object]:
    return build_receipt(
        candidate_dir=paths["candidates"],
        published_dir=paths["published"],
        before_query=paths["before_query"],
        after_query=paths["after_query"],
        before_plan_path=paths["before_plan"],
        observed_plan_path=paths["observed_plan"],
        validation_receipt=paths["validation_receipt"],
        approval_binding=paths["approval_binding"],
        release_tag="v1.2.3",
        source_sha="a" * 40,
        candidate_run_id=101,
        promotion_run_id=202,
        promotion_run_attempt=3,
        channel=channel,
        published_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
    )


def test_marketplace_verification_receipt_binds_all_public_bytes_and_inputs(
    tmp_path: Path,
) -> None:
    receipt = _build(_fixture(tmp_path))
    assert receipt["schema_name"] == "vscode-marketplace-publication-verification"
    assert receipt["promotion_run_attempt"] == 3
    assert receipt["published_at"] == "2026-07-30T12:00:00Z"
    packages = receipt["packages"]
    assert isinstance(packages, dict)
    assert set(packages) == set(SUPPORTED_PLATFORM_TARGETS)
    assert all(
        value["candidate_sha256"] == value["published_sha256"] for value in packages.values()
    )


def test_marketplace_verification_rejects_public_byte_substitution(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    (paths["published"] / "vscode-alysis-linux-x64.vsix").write_bytes(b"substituted")
    with pytest.raises(MarketplaceVerificationError, match="differs from the exact candidate"):
        _build(paths)


def test_marketplace_version_is_independent_of_bundled_cli_release_tag(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    for name in ("before_plan", "observed_plan"):
        plan = json.loads(paths[name].read_text(encoding="utf-8"))
        plan["version"] = "0.2.0"
        paths[name].write_text(json.dumps(plan), encoding="utf-8")
    receipt = _build(paths)
    assert receipt["release_tag"] == "v1.2.3"
    assert receipt["channel"] == "vscode-marketplace-stable"


def test_beta_publication_receipt_rejects_a_stable_observation(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    for name in ("before_plan", "observed_plan"):
        plan = json.loads(paths[name].read_text(encoding="utf-8"))
        plan.update(channel="beta", version="0.3.0")
        paths[name].write_text(json.dumps(plan), encoding="utf-8")
    assert _build(paths, channel="beta")["channel"] == "vscode-marketplace-beta"
    with pytest.raises(MarketplaceVerificationError, match="one release candidate"):
        _build(paths)
    observed = json.loads(paths["observed_plan"].read_text(encoding="utf-8"))
    observed["channel"] = "stable"
    paths["observed_plan"].write_text(json.dumps(observed), encoding="utf-8")
    with pytest.raises(MarketplaceVerificationError, match="one release candidate"):
        _build(paths, channel="beta")


def test_marketplace_verification_rejects_incomplete_observed_plan(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    plan = json.loads(paths["observed_plan"].read_text(encoding="utf-8"))
    plan["missing"] = [{"target": "linux-x64", "candidate": "vscode-alysis-linux-x64.vsix"}]
    paths["observed_plan"].write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(MarketplaceVerificationError, match="incomplete"):
        _build(paths)


def test_marketplace_verification_rejects_candidate_or_validation_receipt_mutation(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    receipt = _build(paths)
    original = receipt["validation_receipt_sha256"]
    paths["validation_receipt"].write_text('{"receipt":"changed"}', encoding="utf-8")
    assert _build(paths)["validation_receipt_sha256"] != original
    (paths["candidates"] / "vscode-alysis-win32-x64.vsix").write_bytes(b"changed")
    with pytest.raises(MarketplaceVerificationError, match="exact win32-x64 candidate"):
        _build(paths)
