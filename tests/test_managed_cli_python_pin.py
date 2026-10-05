import hashlib
from pathlib import Path

import pytest

from scripts.release.install_managed_cli_python import validate_runtime, verify_archive


def test_distribution_is_verified_before_extraction(tmp_path: Path) -> None:
    archive = tmp_path / "python.tar.gz"
    archive.write_bytes(b"correct distribution")
    pin = {
        "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    verify_archive(archive, pin)
    archive.write_bytes(b"altered distribution")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_archive(archive, pin)
    archive.write_bytes(b"short")
    with pytest.raises(ValueError, match="size"):
        verify_archive(archive, pin)


@pytest.mark.parametrize("machine", ["AMD64", "x86_64", "i386"])
def test_arm64_cannot_be_satisfied_by_emulated_intel_python(machine: str) -> None:
    with pytest.raises(ValueError, match="native target"):
        validate_runtime(
            {"version": "3.12.15", "platform": "win32", "machine": machine},
            "win32-arm64",
            "3.12.15",
        )


def test_unpatched_runtime_cannot_satisfy_the_version_pin() -> None:
    with pytest.raises(ValueError, match="version"):
        validate_runtime(
            {"version": "3.12.13", "platform": "linux", "machine": "aarch64"},
            "linux-arm64",
            "3.12.15",
        )
    validate_runtime(
        {"version": "3.12.15", "platform": "linux", "machine": "aarch64"}, "linux-arm64", "3.12.15"
    )
