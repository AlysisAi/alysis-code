#!/usr/bin/env python3
"""Plan or verify no-clobber GitHub Release asset reconciliation."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


class ReleaseAssetReconciliationError(RuntimeError):
    """Remote release assets conflict with the validated local bundle."""


def reconcile_release_assets(
    local_dir: Path,
    remote_dir: Path,
    *,
    require_complete: bool = False,
) -> tuple[str, ...]:
    local = _regular_inventory(local_dir, label="local")
    remote = _regular_inventory(remote_dir, label="downloaded release")
    unexpected = sorted(set(remote) - set(local))
    if unexpected:
        raise ReleaseAssetReconciliationError(
            f"Downloaded release inventory contains unexpected assets: {unexpected}"
        )
    conflicts = sorted(name for name in remote if _sha256(remote[name]) != _sha256(local[name]))
    if conflicts:
        raise ReleaseAssetReconciliationError(
            "Existing GitHub Release assets have different bytes and will not be overwritten: "
            + ", ".join(conflicts)
        )
    missing = tuple(sorted(set(local) - set(remote)))
    if require_complete and missing:
        raise ReleaseAssetReconciliationError(
            "GitHub Release asset publication is incomplete: " + ", ".join(missing)
        )
    return missing


def _regular_inventory(directory: Path, *, label: str) -> dict[str, Path]:
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise ReleaseAssetReconciliationError(f"{label.title()} directory is unavailable.") from exc
    if any(not entry.is_file() or entry.is_symlink() for entry in entries):
        raise ReleaseAssetReconciliationError(
            f"{label.title()} inventory must contain regular files only."
        )
    return {entry.name: entry for entry in entries}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_dir", type=Path)
    parser.add_argument("remote_dir", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--write-missing", type=Path)
    args = parser.parse_args(argv)
    try:
        missing = reconcile_release_assets(
            args.local_dir,
            args.remote_dir,
            require_complete=args.require_complete,
        )
    except ReleaseAssetReconciliationError as exc:
        print(f"GitHub Release asset reconciliation failed: {exc}", file=sys.stderr)
        return 1
    if args.write_missing:
        args.write_missing.write_text("".join(f"{name}\n" for name in missing), encoding="utf-8")
    print(
        "GitHub Release assets are complete"
        if not missing
        else "GitHub Release assets missing safely: " + ", ".join(missing)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
