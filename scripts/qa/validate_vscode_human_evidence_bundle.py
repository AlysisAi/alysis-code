#!/usr/bin/env python3
"""Validate the closed ZIP containing every artifact referenced by human VS Code QA."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile, ZipInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.hash_vscode_release_evidence import TARGETS  # noqa: E402

BUNDLE_FILENAME = "vscode-alysis-human-check-artifacts.zip"
ENVIRONMENT_REPORT_FILENAME = "vscode-alysis-environment-acceptance.json"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_COUNT = 512
MAX_COMPRESSION_RATIO = 200


class HumanEvidenceBundleError(ValueError):
    """Raised when human evidence artifacts are unsafe, missing, or unreferenced."""


def _json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise HumanEvidenceBundleError(f"{label} must be a regular JSON file: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HumanEvidenceBundleError(f"{label} is not valid UTF-8 JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise HumanEvidenceBundleError(f"{label} must be a JSON object: {path.name}")
    return value


def _receipt_digest(receipt: object, label: str) -> str:
    if not isinstance(receipt, dict):
        raise HumanEvidenceBundleError(f"{label} must be a structured receipt")
    digest = receipt.get("artifact_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise HumanEvidenceBundleError(f"{label}.artifact_sha256 must be lowercase SHA-256")
    return digest


def referenced_artifact_hashes(evidence_dir: Path) -> set[str]:
    """Collect the exact unique artifact hashes from all reviewed human reports."""

    referenced: set[str] = set()
    for target in sorted(TARGETS):
        path = evidence_dir / f"vscode-alysis-{target}.manual-provider.json"
        report = _json_object(path, f"{target} manual-provider report")
        checks = report.get("completed_checks")
        if not isinstance(checks, dict) or not checks:
            raise HumanEvidenceBundleError(
                f"{target} manual-provider report has no completed_checks"
            )
        for check_id, receipt in sorted(checks.items()):
            referenced.add(_receipt_digest(receipt, f"{target}.completed_checks.{check_id}"))

    environment = _json_object(
        evidence_dir / ENVIRONMENT_REPORT_FILENAME,
        "environment acceptance report",
    )
    environments = environment.get("environments")
    if not isinstance(environments, dict) or not environments:
        raise HumanEvidenceBundleError("environment acceptance report has no environments")
    for environment_name, entry in sorted(environments.items()):
        checks = entry.get("completed_checks") if isinstance(entry, dict) else None
        if not isinstance(checks, dict) or not checks:
            raise HumanEvidenceBundleError(
                f"environment {environment_name!r} has no completed_checks"
            )
        for check_id, receipt in sorted(checks.items()):
            referenced.add(
                _receipt_digest(
                    receipt,
                    f"environments.{environment_name}.completed_checks.{check_id}",
                )
            )
    if not referenced:
        raise HumanEvidenceBundleError("human evidence reports reference no artifacts")
    return referenced


def _safe_member(info: ZipInfo) -> None:
    name = info.filename
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != "artifacts"
        or any(part in {"", ".", ".."} for part in path.parts)
        or name.endswith("/")
    ):
        raise HumanEvidenceBundleError(f"ZIP member path is unsafe: {name!r}")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in (0, stat.S_IFREG):
        raise HumanEvidenceBundleError(f"ZIP member is not a regular file: {name!r}")
    if info.flag_bits & 0x1:
        raise HumanEvidenceBundleError(f"encrypted ZIP members are not allowed: {name!r}")
    if info.file_size <= 0:
        raise HumanEvidenceBundleError(f"ZIP member must not be empty: {name!r}")
    if info.file_size > MAX_MEMBER_BYTES:
        raise HumanEvidenceBundleError(f"ZIP member exceeds the safe size limit: {name!r}")
    if info.compress_size <= 0 or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO:
        raise HumanEvidenceBundleError(f"ZIP member has an unsafe compression ratio: {name!r}")


def validate_bundle(bundle_path: Path, evidence_dir: Path) -> dict[str, str]:
    """Return member-to-digest mappings after closed, bounded ZIP validation."""

    if bundle_path.name != BUNDLE_FILENAME:
        raise HumanEvidenceBundleError(f"human evidence ZIP must be named {BUNDLE_FILENAME}")
    if not bundle_path.is_file() or bundle_path.is_symlink():
        raise HumanEvidenceBundleError("human evidence ZIP must be a regular file")
    archive_size = bundle_path.stat().st_size
    if archive_size <= 0 or archive_size > MAX_ARCHIVE_BYTES:
        raise HumanEvidenceBundleError("human evidence ZIP size is unsafe")
    expected = referenced_artifact_hashes(evidence_dir)
    try:
        with ZipFile(bundle_path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_MEMBER_COUNT:
                raise HumanEvidenceBundleError("human evidence ZIP member count is unsafe")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or len(names) != len(
                {name.casefold() for name in names}
            ):
                raise HumanEvidenceBundleError("human evidence ZIP contains duplicate member names")
            total = sum(info.file_size for info in infos)
            if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise HumanEvidenceBundleError(
                    "human evidence ZIP exceeds the total uncompressed size limit"
                )
            observed: dict[str, str] = {}
            seen_digests: set[str] = set()
            for info in infos:
                _safe_member(info)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > MAX_MEMBER_BYTES:
                            raise HumanEvidenceBundleError(
                                f"ZIP member exceeds the safe size limit: {info.filename!r}"
                            )
                        digest.update(chunk)
                if size != info.file_size:
                    raise HumanEvidenceBundleError(
                        f"ZIP member size changed while reading: {info.filename!r}"
                    )
                rendered = digest.hexdigest()
                if rendered in seen_digests:
                    raise HumanEvidenceBundleError(
                        "human evidence ZIP contains duplicate artifact bytes"
                    )
                seen_digests.add(rendered)
                observed[info.filename] = rendered
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise HumanEvidenceBundleError("human evidence ZIP is invalid or unreadable") from exc

    observed_hashes = set(observed.values())
    if observed_hashes != expected:
        missing = sorted(expected - observed_hashes)
        unreferenced = sorted(observed_hashes - expected)
        raise HumanEvidenceBundleError(
            "human evidence ZIP hash inventory mismatch; "
            f"missing={missing}, unreferenced={unreferenced}"
        )
    return observed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("evidence_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        observed = validate_bundle(args.bundle, args.evidence_dir)
    except HumanEvidenceBundleError as exc:
        parser.error(str(exc))
    print(f"Validated {len(observed)} closed human evidence artifact(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
