#!/usr/bin/env python3
"""Compute or verify the canonical inventory digest of VS Code release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

TARGETS = (
    "win32-x64",
    "win32-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64",
    "linux-arm64",
)
EXPECTED_FILENAMES = frozenset(
    {f"vscode-alysis-{target}.manual-provider.json" for target in TARGETS}
    | {
        "vscode-alysis-environment-acceptance.json",
        "vscode-alysis-human-check-artifacts.zip",
        "vscode-alysis-production-signoff.json",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class EvidenceInventoryError(ValueError):
    """Raised when the reviewed evidence inventory is incomplete or unsafe."""


def evidence_inventory(directory: Path) -> dict[str, object]:
    if not directory.is_dir() or directory.is_symlink():
        raise EvidenceInventoryError(f"evidence directory is missing or unsafe: {directory}")
    entries = [path for path in directory.iterdir()]
    observed = {path.name for path in entries}
    if observed != EXPECTED_FILENAMES:
        missing = sorted(EXPECTED_FILENAMES - observed)
        extra = sorted(observed - EXPECTED_FILENAMES)
        raise EvidenceInventoryError(
            f"evidence inventory mismatch: missing={missing}, extra={extra}"
        )
    files: list[dict[str, object]] = []
    for path in sorted(entries, key=lambda item: item.name):
        if not path.is_file() or path.is_symlink():
            raise EvidenceInventoryError(f"evidence entry must be a regular file: {path.name}")
        content = path.read_bytes()
        files.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_name": "vscode-release-evidence-inventory",
        "schema_version": 1,
        "files": files,
        "inventory_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expect")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        inventory = evidence_inventory(args.directory)
        if args.expect is not None:
            if SHA256_RE.fullmatch(args.expect) is None:
                raise EvidenceInventoryError("expected inventory digest must be lowercase SHA-256")
            if inventory["inventory_sha256"] != args.expect:
                raise EvidenceInventoryError(
                    "evidence inventory digest does not match reviewed input"
                )
        if args.output is not None:
            args.output.write_text(
                json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    except (EvidenceInventoryError, OSError) as exc:
        print(f"VS Code evidence inventory validation failed: {exc}", file=sys.stderr)
        return 1
    print(inventory["inventory_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
