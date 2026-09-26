from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.qa import validate_vscode_environment_acceptance as validator

SOURCE_SHA = "a" * 40
RELEASE_TAG = "v1.2.3"
CANDIDATE_RUN_ID = 12345


def _candidate_directory(tmp_path: Path) -> Path:
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    for target in sorted(set(validator.DEFAULT_TARGETS.values())):
        vsix = candidate_dir / f"vscode-alysis-{target}.vsix"
        vsix.write_bytes(f"candidate:{target}".encode())
        digest = hashlib.sha256(vsix.read_bytes()).hexdigest()
        (candidate_dir / f"production-dogfood-{target}.json").write_text(
            json.dumps(
                {
                    "platform_target": target,
                    "vsix_sha256": digest,
                    "managed_artifact_version": "1.2.3",
                    "managed_cli_sha256": hashlib.sha256(f"runtime:{target}".encode()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
    return candidate_dir


def _completed_report(candidate_dir: Path) -> dict[str, object]:
    report = validator.build_template(
        candidate_dir,
        release_tag=RELEASE_TAG,
        source_sha=SOURCE_SHA,
        candidate_run_id=CANDIDATE_RUN_ID,
    )
    report.update(
        {
            "status": "completed",
            "reviewer": "release-reviewer",
            "started_at": "2026-07-30T16:00:00Z",
            "completed_at": "2026-07-30T17:00:00Z",
        }
    )
    environments = report["environments"]
    assert isinstance(environments, dict)
    for entry in environments.values():
        assert isinstance(entry, dict)
        entry.update(
            {
                "status": "passed",
                "vscode_version": "1.109.0",
                "vscode_commit": "b" * 40,
                "host_description": "ephemeral release acceptance host",
                "provider": "approved-live-provider",
            }
        )
        checks = entry["completed_checks"]
        assert isinstance(checks, dict)
        entry["completed_checks"] = {
            check_id: {
                "status": "passed",
                "completed_at": "2026-07-30T16:30:00Z",
                "artifact_sha256": hashlib.sha256(check_id.encode()).hexdigest(),
                "event_id": "",
            }
            for check_id in checks
        }
    environments["wsl"]["workspace_authority"] = "wsl+Ubuntu-24.04"
    environments["remote_ssh"]["workspace_authority"] = "ssh-remote+ephemeral-host"
    return report


def _validate(report: dict[str, object], candidate_dir: Path) -> None:
    validator.validate_report(
        report,
        candidate_dir,
        expected_release_tag=RELEASE_TAG,
        expected_source_sha=SOURCE_SHA,
        expected_candidate_run_id=CANDIDATE_RUN_ID,
    )


def test_completed_environment_matrix_binds_all_required_shapes(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)

    _validate(report, candidate_dir)

    environments = report["environments"]
    assert isinstance(environments, dict)
    assert set(environments) == set(validator.REQUIRED_ENVIRONMENTS)
    assert environments["wsl"]["remote_name"] == "wsl"
    assert environments["remote_ssh"]["remote_name"] == "ssh-remote"
    assert environments["untrusted_workspace"]["workspace_trusted"] is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("release_tag", "v9.9.9", "release_tag"),
        ("source_sha", "b" * 40, "source_sha"),
        ("candidate_run_id", 999, "candidate_run_id"),
        ("reviewer", "TBD", "reviewer"),
    ],
)
def test_environment_matrix_rejects_replayed_release_identity(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    report[field] = value

    with pytest.raises(validator.EnvironmentAcceptanceError, match=message):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_missing_environment(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    del environments["remote_ssh"]

    with pytest.raises(validator.EnvironmentAcceptanceError, match="exact required environment"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_unknown_schema_fields(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    report["unreviewed_claim"] = "passed"

    with pytest.raises(validator.EnvironmentAcceptanceError, match="exact schema-v3 fields"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_candidate_substitution(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    environments["wsl"]["vsix_sha256"] = "f" * 64

    with pytest.raises(validator.EnvironmentAcceptanceError, match="exact candidate VSIX"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_wrong_remote_runtime_target(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    remote = environments["remote_ssh"]
    windows = environments["windows_local"]
    for field in (
        "platform_target",
        "vsix",
        "vsix_sha256",
        "managed_artifact_version",
        "managed_cli_sha256",
    ):
        remote[field] = windows[field]

    with pytest.raises(validator.EnvironmentAcceptanceError, match="Linux candidate"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_untrusted_waiver(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    environments["untrusted_workspace"]["completed_checks"]["mutating_paths_denied"]["status"] = (
        "not_applicable"
    )

    with pytest.raises(validator.EnvironmentAcceptanceError, match="status must be passed"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_bare_check_status(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    environments["wsl"]["completed_checks"]["bridge_health"] = "passed"

    with pytest.raises(validator.EnvironmentAcceptanceError, match="structured receipt"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_free_form_event_locator(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    receipt = environments["wsl"]["completed_checks"]["bridge_health"]
    receipt["event_id"] = "acceptance/bridge/0001"

    with pytest.raises(validator.EnvironmentAcceptanceError, match="free-form event_id"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_receipt_outside_report_interval(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    environments["remote_ssh"]["completed_checks"]["normal_chat"]["completed_at"] = (
        "2026-07-30T18:00:00Z"
    )

    with pytest.raises(validator.EnvironmentAcceptanceError, match="report interval"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_host_target_mismatch(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    environments = report["environments"]
    assert isinstance(environments, dict)
    environments["windows_local"]["host_platform"] = "linux"

    with pytest.raises(validator.EnvironmentAcceptanceError, match="OS/architecture"):
        _validate(report, candidate_dir)


def test_environment_matrix_rejects_secret_looking_values(tmp_path: Path) -> None:
    candidate_dir = _candidate_directory(tmp_path)
    report = _completed_report(candidate_dir)
    leaked = copy.deepcopy(report)
    environments = leaked["environments"]
    assert isinstance(environments, dict)
    environments["linux_local"]["host_description"] = "token=abcdefghijklmnop"

    with pytest.raises(validator.EnvironmentAcceptanceError, match="secret-looking"):
        _validate(leaked, candidate_dir)
