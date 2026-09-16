from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from alysis_code import feedback_report as reports
from alysis_code.config import AppConfig


def session(root: Path, **overrides):
    values = dict(
        root=root,
        messages=[],
        store=SimpleNamespace(enabled=False, path=None, session_id="report_test"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "authorization",
        "password",
        "access_token",
        "refreshToken",
        "ANTHROPIC_API_KEY",
        "client_secret",
        "cookie",
        "credentials",
    ],
)
def test_structured_credentials_removed_before_serialization(tmp_path, key):
    secret = "synthetic_Z7vQ2mA9pL4rT8bN6xK3"
    payload = {"nested": [{key: secret}], "total_tokens": 123, "reasoning_tokens": 7}
    result = reports.create_feedback_bundle(
        workspace_root=tmp_path,
        active_session=session(tmp_path, messages=[{"role": "user", "content": payload}]),
    )
    with zipfile.ZipFile(result.zip_path) as archive:
        exported = archive.read("session/session_snapshot.json").decode()
    assert secret not in exported
    assert "[REDACTED]" in exported
    assert json.loads(exported)["messages"][0]["content"]["total_tokens"] == 123


@pytest.mark.parametrize(
    "assignment",
    [
        'password="short secret"',
        "password=short",
        "token=synthetic_Z7vQ2mA9pL4rT8bN6xK3",
        "GEMINI_API_KEY=synthetic_Z7vQ2mA9pL4rT8bN6xK3",
    ],
)
def test_feedback_redacted_in_archive_and_browser_url(tmp_path, assignment):
    result = reports.create_feedback_bundle(
        workspace_root=tmp_path, active_session=session(tmp_path), feedback_text=assignment
    )
    opened = []
    reports.create_feedback_github_issue_draft(
        bundle_result=result,
        feedback_text=assignment,
        cfg=AppConfig(),
        browser_open=lambda url, **kwargs: opened.append(url) or True,
    )
    body = parse_qs(urlsplit(opened[0]).query)["body"][0]
    feedback = (result.bundle_dir / "feedback.md").read_text()
    secret = assignment.split("=", 1)[1].strip('"')
    assert secret not in body
    assert secret not in feedback


def test_known_secret_value_redacted_even_without_assignment(tmp_path, monkeypatch):
    secret = "synthetic_M8wX1bN7rK3aL5qV9pZ2"
    monkeypatch.setenv("SYNTHETIC_API_KEY", secret)
    assert secret not in reports.sanitize_feedback_text(
        f"Failure with {secret}", workspace_root=tmp_path
    )


def test_embedded_json_redacted_in_archive_feedback_and_github_draft(tmp_path):
    secret = "synthetic-secret-not-in-environment-49183"
    # Escape the key too: inspect the decoded JSON rather than its wire spelling.
    content = '{"\\u0070assword": "' + secret + '", "enabled": true}'
    arguments = json.dumps({"path": "config.json", "content": content})
    result = reports.create_feedback_bundle(
        workspace_root=tmp_path,
        active_session=session(tmp_path, messages=[{"role": "tool", "content": arguments}]),
        feedback_text=arguments,
    )
    with zipfile.ZipFile(result.zip_path) as archive:
        for name in archive.namelist():
            assert secret.encode() not in archive.read(name)
        snapshot = json.loads(archive.read("session/session_snapshot.json"))
    decoded_arguments = json.loads(snapshot["messages"][0]["content"])
    assert json.loads(decoded_arguments["content"]) == {
        "password": "[REDACTED]",
        "enabled": True,
    }
    draft = reports.create_feedback_github_issue_draft(
        bundle_result=result, feedback_text=arguments, cfg=AppConfig(), open_browser=False
    )
    assert secret not in parse_qs(urlsplit(draft.issue_url).query)["body"][0]


@pytest.mark.parametrize("depth", [80, 2000])
def test_excessively_nested_json_is_omitted_without_leaking_credentials(tmp_path, depth):
    secret = "synthetic-secret-not-in-environment-49183"
    content = "[" * depth + json.dumps({"password": secret}) + "]" * depth
    cleaned = reports.sanitize_feedback_text(content, workspace_root=tmp_path)
    assert secret not in cleaned
    assert "Omitted" in cleaned


@pytest.mark.parametrize(
    "message", ["hello", "Unicode: \u03b1\u03b2\u03b3", "Escaped surrogate: \ud800"]
)
def test_json_strings_keep_their_type_and_nonsecret_values(tmp_path, message):
    payload = {"message": json.dumps(message), "count": 3, "enabled": True, "items": [None]}
    cleaned = reports.sanitize_feedback_text(json.dumps(payload), workspace_root=tmp_path)
    assert json.loads(cleaned.encode("utf-8")) == payload


def test_cancel_during_compression_cleans_partial_bundle_and_keeps_previous(tmp_path):
    previous = reports.create_feedback_bundle(
        workspace_root=tmp_path, active_session=session(tmp_path)
    )
    cancel = False

    def progress(label):
        nonlocal cancel
        if label == "Compressing support bundle":
            cancel = True

    with pytest.raises(reports.FeedbackReportCancelled):
        reports.create_feedback_bundle(
            workspace_root=tmp_path,
            active_session=session(tmp_path),
            progress=progress,
            cancelled=lambda: cancel,
        )
    assert previous.zip_path.exists()
    assert sorted(p.name for p in previous.output_root.iterdir()) == sorted(
        [previous.bundle_dir.name, previous.zip_path.name]
    )


def test_io_failure_becomes_retryable_report_error_and_removes_partial_output(
    tmp_path, monkeypatch
):
    def fail_zip(*, bundle_dir, zip_path):
        zip_path.with_suffix(".zip.partial").write_bytes(b"incomplete")
        raise OSError("disk full")

    monkeypatch.setattr(reports, "_write_deterministic_zip", fail_zip)
    with pytest.raises(reports.FeedbackReportError, match="free space"):
        reports.create_feedback_bundle(workspace_root=tmp_path, active_session=session(tmp_path))
    assert list((tmp_path / "alysis-feedback").iterdir()) == []


def test_non_utf8_artifact_does_not_bypass_redaction(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    secret = "synthetic_Z7vQ2mA9pL4rT8bN6xK3"
    (artifacts / "output.txt").write_bytes(("password=" + secret).encode("utf-16"))
    active = session(
        tmp_path,
        store=SimpleNamespace(
            enabled=False, path=None, session_id="binary", session_artifact_root=artifacts
        ),
    )
    result = reports.create_feedback_bundle(workspace_root=tmp_path, active_session=active)
    content = (result.bundle_dir / "session/artifacts/output.txt").read_text()
    assert "Omitted" in content
    assert secret not in content


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path regression")
def test_windows_deep_export_destination_is_supported(tmp_path):
    artifacts = tmp_path / "artifacts"
    nested = artifacts / ("a" * 60) / ("b" * 60) / ("c" * 60)
    reports._io_path(nested).mkdir(parents=True)
    reports._io_path(nested / "output.json").write_text('{"api_key":"synthetic-secret", "ok":true}')
    active = session(
        tmp_path,
        store=SimpleNamespace(
            enabled=False,
            path=None,
            session_id="long_windows_paths",
            session_artifact_root=artifacts,
        ),
    )
    result = reports.create_feedback_bundle(workspace_root=tmp_path, active_session=active)
    exported = (
        result.bundle_dir / "session/artifacts" / nested.relative_to(artifacts) / "output.json"
    )
    assert len(str(exported)) > 260
    assert json.loads(reports._io_path(exported).read_text())["api_key"] == "[REDACTED]"
    with zipfile.ZipFile(result.zip_path) as archive:
        assert archive.testzip() is None
        assert not any("\\\\?\\" in name for name in archive.namelist())
