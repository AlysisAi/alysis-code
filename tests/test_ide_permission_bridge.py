from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.stdio_bridge import ApprovalScopeRecord, StdioBridge
from alysis_code.permission_policy import PermissionPolicyStore


def _request(method: str, params: dict[str, Any], request_id: str) -> str:
    return (
        json.dumps(
            {
                "protocol_version": "1",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        + "\n"
    )


def _response(output: io.StringIO, request_id: str) -> dict[str, Any]:
    return next(
        json.loads(line)
        for line in output.getvalue().splitlines()
        if json.loads(line).get("id") == request_id
    )


def test_permission_rule_protocol_is_explainable_and_redacts_commands(tmp_path: Path) -> None:
    output = io.StringIO()
    bridge = StdioBridge(
        stdout=output,
        permission_policy_store=PermissionPolicyStore(tmp_path / "permission.json"),
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )

    bridge.process_line(
        _request(
            "permission.rules.grant",
            {
                "effect": "allow",
                "tool_pattern": "shell_run",
                "command_pattern": "pytest tests/*",
                "confirm": True,
            },
            "grant",
        )
    )
    granted = _response(output, "grant")["result"]["rule"]
    assert granted["has_command_pattern"] is True
    assert "command_pattern" not in granted

    bridge.process_line(_request("permission.rules.list", {}, "list"))
    listed = _response(output, "list")["result"]
    assert listed["command_patterns_redacted"] is True
    assert "pytest" not in json.dumps(listed)

    bridge.process_line(
        _request(
            "permission.evaluate",
            {"tool_name": "shell_run", "command": "pytest tests/unit"},
            "evaluate",
        )
    )
    evaluation = _response(output, "evaluate")["result"]
    assert evaluation["decision"] == "allow"
    assert evaluation["matched_rule_id"] == granted["id"]
    assert evaluation["reason"] == "matched_rule"

    bridge.process_line(
        _request(
            "permission.rules.revoke",
            {"rule_id": granted["id"], "yes": True},
            "revoke",
        )
    )
    assert _response(output, "revoke")["result"]["status"] == "revoked"
    bridge.close()


def test_permission_sensitive_override_and_session_grant_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="test-model"))

    class FakeSession:
        store = SimpleNamespace(session_artifact_root=tmp_path / "artifacts")

        def close(self) -> None:
            return

    output = io.StringIO()
    store = PermissionPolicyStore(tmp_path / "permission.json")
    store.grant("allow", tool_pattern="fs_write")
    bridge = StdioBridge(
        stdout=output,
        create_session_fn=lambda **_kwargs: FakeSession(),
        permission_policy_store=store,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": os.fspath(workspace),
                "mode": "review",
                "model": "test-model",
                "session_id": "permission-session",
            },
            "create",
        )
    )

    bridge.process_line(
        _request(
            "permission.evaluate",
            {
                "tool_name": "fs_write",
                "workspace": os.fspath(workspace),
                "paths": [".env"],
            },
            "sensitive",
        )
    )
    assert _response(output, "sensitive")["result"] == {
        "decision": "ask",
        "reason": "sensitive_resource_requires_approval",
        "matched_rule_id": "override:sensitive",
        "matched_rule_source": "builtin_safety",
        "specificity": 2_147_483_647,
    }

    session = bridge._sessions["permission-session"]
    session.approved_approval_scopes.append(
        ApprovalScopeRecord(
            kind="fs_write",
            scope={"type": "exact_file_set", "kind": "fs_write", "files": ["safe.txt"]},
            key="test-key",
        )
    )
    bridge.process_line(
        _request("permission.session.list", {"session_id": session.session_id}, "grants")
    )
    grants = _response(output, "grants")["result"]["grants"]
    assert len(grants) == 1
    assert "files" not in json.dumps(grants)

    bridge.process_line(
        _request(
            "permission.session.revoke",
            {"session_id": session.session_id, "grant_id": grants[0]["id"]},
            "session-revoke",
        )
    )
    assert _response(output, "session-revoke")["result"]["status"] == "revoked"
    assert session.approved_approval_scopes == []
    bridge.close()


def test_permission_evaluate_detects_workspace_symlink_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    output = io.StringIO()
    store = PermissionPolicyStore(tmp_path / "permission.json")
    store.grant("allow", tool_pattern="fs_write", path_pattern="linked/**")
    bridge = StdioBridge(
        stdout=output,
        permission_policy_store=store,
        prompt_queue=DurablePromptQueue(tmp_path / "queue.sqlite3"),
    )

    bridge.process_line(
        _request(
            "permission.evaluate",
            {
                "tool_name": "fs_write",
                "workspace": os.fspath(workspace),
                "paths": ["linked/output.txt"],
            },
            "symlink-escape",
        )
    )

    result = _response(output, "symlink-escape")["result"]
    assert result["decision"] == "ask"
    assert result["reason"] == "external_directory_requires_approval"
    assert result["matched_rule_id"] == "override:external_directory"
    bridge.close()
