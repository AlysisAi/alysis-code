from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from scripts.qa import validate_vscode_human_evidence_bundle as validator


def _reports(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    artifacts: dict[str, bytes] = {}
    for target in validator.TARGETS:
        content = f"sanitized manual evidence for {target}\n".encode()
        digest = hashlib.sha256(content).hexdigest()
        artifacts[digest] = content
        (evidence / f"vscode-alysis-{target}.manual-provider.json").write_text(
            json.dumps(
                {
                    "completed_checks": {
                        "normal_chat": {"artifact_sha256": digest},
                        "reload": {"artifact_sha256": digest},
                    }
                }
            ),
            encoding="utf-8",
        )
    environment_content = b"sanitized environment evidence\n"
    environment_digest = hashlib.sha256(environment_content).hexdigest()
    artifacts[environment_digest] = environment_content
    (evidence / validator.ENVIRONMENT_REPORT_FILENAME).write_text(
        json.dumps(
            {
                "environments": {
                    "windows_local": {
                        "completed_checks": {
                            "bridge_health": {"artifact_sha256": environment_digest}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return evidence, artifacts


def _bundle(evidence: Path, artifacts: dict[str, bytes]) -> Path:
    path = evidence / validator.BUNDLE_FILENAME
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index, content in enumerate(artifacts.values()):
            archive.writestr(f"artifacts/evidence-{index}.txt", content)
    return path


def test_human_evidence_zip_closes_every_referenced_hash(tmp_path: Path) -> None:
    evidence, artifacts = _reports(tmp_path)
    observed = validator.validate_bundle(_bundle(evidence, artifacts), evidence)

    assert set(observed.values()) == set(artifacts)


@pytest.mark.parametrize("mutation", ["missing", "unreferenced", "duplicate_bytes"])
def test_human_evidence_zip_rejects_open_or_duplicate_inventory(
    tmp_path: Path, mutation: str
) -> None:
    evidence, artifacts = _reports(tmp_path)
    values = list(artifacts.values())
    if mutation == "missing":
        values.pop()
    elif mutation == "unreferenced":
        values.append(b"not referenced by any reviewed receipt")
    else:
        values.append(values[0])
    path = evidence / validator.BUNDLE_FILENAME
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index, content in enumerate(values):
            archive.writestr(f"artifacts/evidence-{index}.txt", content)

    with pytest.raises(validator.HumanEvidenceBundleError):
        validator.validate_bundle(path, evidence)


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt"])
def test_human_evidence_zip_rejects_unsafe_paths(tmp_path: Path, member: str) -> None:
    evidence, artifacts = _reports(tmp_path)
    path = evidence / validator.BUNDLE_FILENAME
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index, content in enumerate(artifacts.values()):
            if index == 0:
                info = ZipInfo("placeholder")
                # ZipInfo's constructor normalizes the host separator on Windows; assigning the
                # raw field models the hostile central-directory name the validator must reject.
                info.filename = member
                archive.writestr(info, content)
            else:
                archive.writestr(f"artifacts/evidence-{index}.txt", content)

    with pytest.raises(validator.HumanEvidenceBundleError, match="path is unsafe"):
        validator.validate_bundle(path, evidence)


def test_human_evidence_zip_rejects_raw_backslash_member() -> None:
    info = ZipInfo("placeholder")
    info.filename = "artifacts\\evil.txt"
    info.file_size = 1
    info.compress_size = 1

    with pytest.raises(validator.HumanEvidenceBundleError, match="path is unsafe"):
        validator._safe_member(info)


def test_human_evidence_zip_rejects_symlink_member(tmp_path: Path) -> None:
    evidence, artifacts = _reports(tmp_path)
    path = evidence / validator.BUNDLE_FILENAME
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index, content in enumerate(artifacts.values()):
            if index == 0:
                info = ZipInfo("artifacts/link.txt")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, content)
            else:
                archive.writestr(f"artifacts/evidence-{index}.txt", content)

    with pytest.raises(validator.HumanEvidenceBundleError, match="not a regular file"):
        validator.validate_bundle(path, evidence)


def test_human_evidence_zip_rejects_zip_bomb_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, artifacts = _reports(tmp_path)
    path = _bundle(evidence, artifacts)
    monkeypatch.setattr(validator, "MAX_MEMBER_BYTES", 4)

    with pytest.raises(validator.HumanEvidenceBundleError, match="size limit"):
        validator.validate_bundle(path, evidence)
