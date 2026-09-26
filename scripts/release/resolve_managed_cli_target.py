from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

try:
    from scripts.release.build_managed_cli_manifest import TARGETS
except ModuleNotFoundError:  # Direct script execution puts this directory on sys.path.
    from build_managed_cli_manifest import TARGETS


def target_from_artifact(path: str | Path) -> str:
    """Return the exact managed-runtime target encoded by an artifact filename."""

    name = Path(path).name
    has_exe_suffix = name.endswith(".exe")
    if has_exe_suffix:
        name = name.removesuffix(".exe")
    target = name.removeprefix("alysis-")
    if target not in TARGETS or name == target or has_exe_suffix != target.startswith("win32-"):
        raise ValueError(f"unsupported managed CLI artifact filename: {Path(path).name}")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve and validate a managed CLI artifact target."
    )
    parser.add_argument("artifact")
    args = parser.parse_args(argv)
    try:
        target = target_from_artifact(args.artifact)
    except ValueError as exc:
        parser.error(str(exc))
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
