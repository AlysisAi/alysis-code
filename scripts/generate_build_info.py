#!/usr/bin/env python3
"""Stamp the current commit into ``src/alysis_code/_build_info.py``.

Run this immediately before building a wheel or an sdist::

    python3 scripts/generate_build_info.py
    python3 -m build            # or: uv build / hatch build

Why a script rather than a build-backend hook: the project pins its backend
(``hatchling==1.31.0``) and its build requirements exactly, and the wheel that
carries this stamp is the artifact whose provenance is in question. A generator
that runs inside the build environment would have to be correct in an
environment that is, by construction, the one nobody has inspected. A script
the release job invokes is inspectable, runnable in a bare interpreter, and
fails loudly in the job log rather than silently inside a backend.

The committed ``_build_info.py`` is a dev default reporting an unidentifiable
build. After a release build, restore it::

    git checkout -- src/alysis_code/_build_info.py

so the repository never carries a stamp belonging to some earlier build. The
dirty check deliberately ignores this one file, so stamping a clean tree still
reports ``dirty: no``.

Exit codes: ``0`` stamped, ``1`` refused (with ``--require-clean``, when the
tree is dirty or has no commit).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "src" / "alysis_code" / "build_identity.py"


def _load_build_identity():
    """Load ``build_identity`` by path, without importing the package.

    The package needs Python >= 3.11 and a dozen third-party dependencies; the
    release job stamps the tree before any of that is installed.
    """
    spec = importlib.util.spec_from_file_location("_alysis_build_identity", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(_REPO_ROOT),
        help="Repository to interrogate (default: this checkout).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Where to write _build_info.py (default: next to build_identity.py).",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="Exit 1 instead of stamping when the tree is dirty or has no commit.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Report what would be stamped without writing the file.",
    )
    args = parser.parse_args(argv)

    build_identity = _load_build_identity()
    info = build_identity.generate_build_info(repo_root=args.repo_root)

    if args.require_clean and not info.is_clean:
        print(f"refusing to stamp: {info.describe()}", file=sys.stderr)
        return 1

    if args.print_only:
        print(info.describe())
        return 0

    output = Path(args.output) if args.output else build_identity.default_build_info_path()
    written = build_identity.write_build_info(info, output)
    print(f"{written}: {info.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
