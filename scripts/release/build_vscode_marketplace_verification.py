#!/usr/bin/env python3
"""Build a closed receipt for the exact VSIX bytes observed on VS Code Marketplace."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.vscode_extension_dogfood import (  # noqa: E402
    SUPPORTED_PLATFORM_TARGETS,
)
from scripts.release.reconcile_vscode_marketplace import (  # noqa: E402
    MarketplaceReconciliationError,
    verify_downloaded_packages,
)

SCHEMA_NAME = "vscode-marketplace-publication-verification"
SCHEMA_VERSION = 1
PLAN_KEYS = {
    "schema_name",
    "schema_version",
    "extension_id",
    "channel",
    "version",
    "candidates",
    "existing",
    "missing",
}
PACKAGE_KEYS = {"filename", "candidate_sha256", "published_sha256", "download_url"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
TAG_RE = re.compile(r"v\d+\.\d+\.\d+")


class MarketplaceVerificationError(RuntimeError):
    """Raised when public Marketplace evidence is incomplete or not candidate-bound."""


def _sha256(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise MarketplaceVerificationError(f"Evidence must be a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketplaceVerificationError(f"{label} is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise MarketplaceVerificationError(f"{label} must be a JSON object.")
    return payload


def build_receipt(
    *,
    candidate_dir: Path,
    published_dir: Path,
    before_query: Path,
    after_query: Path,
    before_plan_path: Path,
    observed_plan_path: Path,
    validation_receipt: Path,
    approval_binding: Path,
    release_tag: str,
    source_sha: str,
    candidate_run_id: int,
    promotion_run_id: int,
    promotion_run_attempt: int,
    published_at: datetime | None = None,
    channel: str = "stable",
) -> dict[str, Any]:
    if channel not in {"stable", "beta"}:
        raise MarketplaceVerificationError("Unsupported Marketplace channel.")
    if TAG_RE.fullmatch(release_tag) is None or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise MarketplaceVerificationError("Marketplace release identity is invalid.")
    if min(candidate_run_id, promotion_run_id, promotion_run_attempt) <= 0:
        raise MarketplaceVerificationError("Marketplace workflow identities must be positive.")
    before_plan = _json_object(before_plan_path, "Marketplace before plan")
    observed_plan = _json_object(observed_plan_path, "Marketplace observed plan")
    if set(before_plan) != PLAN_KEYS or set(observed_plan) != PLAN_KEYS:
        raise MarketplaceVerificationError("Marketplace plan fields are not exact.")
    if (
        before_plan.get("schema_name") != "vscode-marketplace-publication-plan"
        or before_plan.get("schema_version") != 2
        or observed_plan.get("schema_name") != "vscode-marketplace-publication-plan"
        or observed_plan.get("schema_version") != 2
        or before_plan.get("channel") != channel
        or observed_plan.get("channel") != channel
        or before_plan.get("extension_id") != "alysisai.vscode-alysis"
        or observed_plan.get("extension_id") != "alysisai.vscode-alysis"
        or re.fullmatch(r"\d+\.\d+\.\d+", str(before_plan.get("version", ""))) is None
        or observed_plan.get("version") != before_plan.get("version")
        or before_plan.get("candidates") != observed_plan.get("candidates")
        or observed_plan.get("missing") != []
    ):
        raise MarketplaceVerificationError(
            "Marketplace plans are incomplete or do not bind one release candidate."
        )
    try:
        verify_downloaded_packages(
            observed_plan, published_dir, require_complete=True, channel=channel
        )
    except MarketplaceReconciliationError as exc:
        raise MarketplaceVerificationError(str(exc)) from exc

    candidates = observed_plan.get("candidates")
    existing = observed_plan.get("existing")
    if not isinstance(candidates, dict) or not isinstance(existing, dict):
        raise MarketplaceVerificationError("Marketplace observed inventory is invalid.")
    if set(candidates) != set(SUPPORTED_PLATFORM_TARGETS) or set(existing) != set(
        SUPPORTED_PLATFORM_TARGETS
    ):
        raise MarketplaceVerificationError("Marketplace target inventory is incomplete.")

    packages: dict[str, dict[str, str]] = {}
    for target in sorted(SUPPORTED_PLATFORM_TARGETS):
        candidate = candidates.get(target)
        public = existing.get(target)
        if (
            not isinstance(candidate, dict)
            or set(candidate) != {"filename", "sha256"}
            or not isinstance(public, dict)
            or set(public) != {"candidate", "download_url", "sha256"}
        ):
            raise MarketplaceVerificationError("Marketplace package records are not exact.")
        filename = candidate.get("filename")
        candidate_sha = candidate.get("sha256")
        if (
            not isinstance(filename, str)
            or filename != f"vscode-alysis-{target}.vsix"
            or not isinstance(candidate_sha, str)
            or SHA256_RE.fullmatch(candidate_sha) is None
            or public.get("candidate") != filename
            or public.get("sha256") != candidate_sha
        ):
            raise MarketplaceVerificationError("Marketplace package identity is invalid.")
        candidate_path = candidate_dir / filename
        published_path = published_dir / filename
        observed_candidate_sha = _sha256(candidate_path)
        observed_published_sha = _sha256(published_path)
        if observed_candidate_sha != candidate_sha or observed_published_sha != candidate_sha:
            raise MarketplaceVerificationError(
                f"Marketplace public bytes do not match the exact {target} candidate."
            )
        download_url = public.get("download_url")
        if not isinstance(download_url, str) or not download_url.startswith("https://"):
            raise MarketplaceVerificationError("Marketplace download URL is invalid.")
        package = {
            "filename": filename,
            "candidate_sha256": observed_candidate_sha,
            "published_sha256": observed_published_sha,
            "download_url": download_url,
        }
        if set(package) != PACKAGE_KEYS:
            raise AssertionError("Marketplace verification package schema drifted.")
        packages[target] = package

    timestamp = published_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise MarketplaceVerificationError("published_at must be timezone-aware.")
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "channel": f"vscode-marketplace-{channel}",
        "release_tag": release_tag,
        "source_sha": source_sha,
        "candidate_run_id": candidate_run_id,
        "promotion_run_id": promotion_run_id,
        "promotion_run_attempt": promotion_run_attempt,
        "published_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "packages": packages,
        "marketplace_before_query_sha256": _sha256(before_query),
        "marketplace_after_query_sha256": _sha256(after_query),
        "marketplace_before_plan_sha256": _sha256(before_plan_path),
        "marketplace_observed_plan_sha256": _sha256(observed_plan_path),
        "validation_receipt_sha256": _sha256(validation_receipt),
        "approval_binding_sha256": _sha256(approval_binding),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("published_dir", type=Path)
    parser.add_argument("before_query", type=Path)
    parser.add_argument("after_query", type=Path)
    parser.add_argument("before_plan", type=Path)
    parser.add_argument("observed_plan", type=Path)
    parser.add_argument("validation_receipt", type=Path)
    parser.add_argument("approval_binding", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidate-run-id", required=True, type=int)
    parser.add_argument("--promotion-run-id", required=True, type=int)
    parser.add_argument("--promotion-run-attempt", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        receipt = build_receipt(
            candidate_dir=args.candidate_dir,
            published_dir=args.published_dir,
            before_query=args.before_query,
            after_query=args.after_query,
            before_plan_path=args.before_plan,
            observed_plan_path=args.observed_plan,
            validation_receipt=args.validation_receipt,
            approval_binding=args.approval_binding,
            release_tag=args.release_tag,
            source_sha=args.source_sha,
            candidate_run_id=args.candidate_run_id,
            promotion_run_id=args.promotion_run_id,
            promotion_run_attempt=args.promotion_run_attempt,
            channel=args.channel,
        )
    except MarketplaceVerificationError as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
