from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release.build_managed_cli_manifest import TARGETS
from scripts.release.resolve_managed_cli_target import target_from_artifact


@pytest.mark.parametrize("target", TARGETS)
def test_target_from_artifact_accepts_every_release_filename(target: str) -> None:
    suffix = ".exe" if target.startswith("win32-") else ""
    assert target_from_artifact(f"managed-runtime/alysis-{target}{suffix}") == target


@pytest.mark.parametrize(
    "filename",
    (
        "alysis-win32-x64.exe.cdx.json",
        "alysis-win32-x64.cdx.json",
        "win32-x64.exe",
        "alysis-win32-riscv64.exe",
        "alysis-linux-x64.exe",
    ),
)
def test_target_from_artifact_rejects_non_runtime_or_unsupported_names(filename: str) -> None:
    with pytest.raises(ValueError, match="unsupported managed CLI artifact filename"):
        target_from_artifact(filename)


def test_cli_prints_the_windows_target_without_the_exe_suffix() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "release" / "resolve_managed_cli_target.py"),
            "managed-runtime/alysis-win32-x64.exe",
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "win32-x64"


def test_both_release_loops_use_the_fail_closed_target_resolver() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    managed_release = (
        repository_root / ".github" / "workflows" / "managed-cli-vsix-release.yml"
    ).read_text(encoding="utf-8")
    promotion = (
        repository_root / ".github" / "workflows" / "vscode-extension-promote.yml"
    ).read_text(encoding="utf-8")
    command = 'scripts/release/resolve_managed_cli_target.py "$executable"'

    assert managed_release.count(command) == 1
    assert promotion.count(command) == 1
    assert 'target="${target#alysis-}"' not in managed_release
    assert 'target="${target#alysis-}"' not in promotion
