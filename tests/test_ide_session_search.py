from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.ide.session_search import (
    SessionSearchLimits,
    past_session_context_block,
    search_workspace_sessions,
)
from alysis_code.session_artifacts import SessionArtifactLayout
from alysis_code.session_store import SessionStore


def _session(sessions: Path, workspace: Path, session_id: str) -> SessionStore:
    return SessionStore(
        enabled=True,
        sessions_dir=sessions,
        session_id=session_id,
        cwd=os.fspath(workspace),
        repo_root=None,
        workspace_root=os.fspath(workspace),
    )


def test_search_is_workspace_scoped_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    canary = "secret-session-canary"
    monkeypatch.setenv("ALYSIS_API_KEY", canary)

    owned = _session(sessions, workspace, "owned-session")
    owned.append(
        "assistant_message",
        {"content": f"The frobnicator failed with token {canary}."},
    )
    owned.close()
    foreign_workspace = _session(sessions, other, "other-session")
    foreign_workspace.append("assistant_message", {"content": "frobnicator elsewhere"})
    foreign_workspace.close()

    result = search_workspace_sessions(
        sessions_dir=sessions,
        workspace_root=workspace,
        query="frobnicator",
        limits=SessionSearchLimits(max_results=5, max_snippet_chars=200),
    )

    assert [item["session_id"] for item in result["results"]] == ["owned-session"]
    assert canary not in json.dumps(result)
    assert result["redacted"] is True
    assert result["secret_values_included"] is False


def test_search_result_converts_to_bounded_past_session_context(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _session(sessions, workspace, "session-one")
    store.append("user_message", {"content": "remember the purple widget"})
    store.close()

    result = search_workspace_sessions(
        sessions_dir=sessions,
        workspace_root=workspace,
        query="purple widget",
    )["results"][0]
    block = past_session_context_block(result)

    assert block["type"] == "past_session"
    assert block["session_id"] == "session-one"
    assert block["provenance"]["source"] == "alysis.session.search"
    assert "purple widget" in block["content"]


def test_search_includes_session_scoped_offload_after_restart_and_redacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    secret = "cross-session-secret-canary"
    monkeypatch.setenv("ALYSIS_API_KEY", secret)

    for session_id, scoped_workspace, marker in (
        ("owned-offload", workspace, "owned-artifact-needle"),
        ("foreign-offload", other, "foreign-artifact-needle"),
    ):
        store = _session(sessions, scoped_workspace, session_id)
        store.append("session_start", {"content": "started"})
        store.close()
        offloader = ToolOutputOffloader(
            artifact_layout=SessionArtifactLayout(filesystem_root=sessions / session_id),
            workspace_root=scoped_workspace,
            threshold_chars=20,
            preview_chars=10,
        )
        output = offloader.maybe_offload(
            tool_name="shell_run",
            tool_call_id="restart-call",
            step=1,
            result={},
            content_json=json.dumps(
                {"stdout": f"{marker}:{secret}:" + "x" * 500},
            ),
        )
        assert output.offloaded is True

    result = search_workspace_sessions(
        sessions_dir=sessions,
        workspace_root=workspace,
        query="artifact-needle",
        limits=SessionSearchLimits(max_results=5, max_snippet_chars=300),
    )

    assert [item["session_id"] for item in result["results"]] == ["owned-offload"]
    match = result["results"][0]
    assert match["source_kind"] == "tool_output"
    assert match["artifact_path"].startswith("session_artifacts/tool_outputs/")
    serialized = json.dumps(result)
    assert "foreign-artifact-needle" not in serialized
    assert secret not in serialized
    assert "<redacted>" in serialized


def test_search_artifacts_respects_total_byte_and_result_limits(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _session(sessions, workspace, "bounded-offload")
    store.append("session_start", {"content": "started"})
    store.close()
    artifact_dir = workspace / ".alysis" / "sessions" / "bounded-offload" / "tool_outputs"
    artifact_dir.mkdir(parents=True)
    for index in range(5):
        (artifact_dir / f"{index}.json").write_text(
            f'{{"content_json":"bounded-needle-{index}-' + "x" * 500 + '"}}',
            encoding="utf-8",
        )

    result = search_workspace_sessions(
        sessions_dir=sessions,
        workspace_root=workspace,
        query="bounded-needle",
        limits=SessionSearchLimits(
            max_results=2,
            max_file_bytes=300,
            max_total_bytes=700,
            max_snippet_chars=80,
        ),
    )

    assert len(result["results"]) == 2
    assert result["scanned_bytes"] <= 700
    assert result["truncated"] is True
    assert all(len(item["snippet"]) <= 82 for item in result["results"])
