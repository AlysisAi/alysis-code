from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.prompt_queue import DurablePromptQueue
from alysis_code.ide.stdio_bridge import BridgeJob, StdioBridge
from alysis_code.mcp.config import load_resolved_mcp_config, user_mcp_config_path
from alysis_code.mcp.errors import McpProcessError
from alysis_code.mcp.manager import McpManager
from alysis_code.runtime_kind import RuntimeKind

_FIXTURE_SERVER = (
    Path(__file__).resolve().parent / "fixtures" / "mcp_servers" / "minimal_stdio_server.py"
)


def _request(method: str, params: dict[str, Any], *, request_id: str) -> str:
    return json.dumps(
        {
            "protocol_version": "1",
            "id": request_id,
            "method": method,
            "params": params,
        },
        sort_keys=True,
    )


def _lines(output: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def _send(
    bridge: StdioBridge,
    output: io.StringIO,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    before = len(_lines(output))
    bridge.process_line(_request(method, params, request_id=method) + "\n")
    return _lines(output)[before]


def _configure_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    fixture_config = tmp_path / "mcp-fixture.json"
    fixture_config.write_text(
        json.dumps(
            {
                "tools_pages": [
                    [
                        {
                            "name": "echo",
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                            },
                        }
                    ]
                ],
                "tool_call_results": {
                    "echo": {
                        "isError": False,
                        "content": [{"type": "text", "text": "ok"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = user_mcp_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "alpha": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [os.fspath(_FIXTURE_SERVER)],
                        "env": {"ALYSIS_TEST_MCP_CONFIG": os.fspath(fixture_config)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class _FakeStore:
    def __init__(self, root: Path) -> None:
        self.session_artifact_root = root / "session-artifacts"
        self.session_artifact_root.mkdir(parents=True, exist_ok=True)


class _FakeManagedBrowser:
    def close_all(self) -> None:
        return


class _FakeAgentSession:
    def __init__(self, *, manager: McpManager, root: Path) -> None:
        self.mcp_manager = manager
        self.store = _FakeStore(root)
        self.tool_output_offloader = None

    def run_turn(self, _message: str) -> int:
        return 0

    def close(self) -> None:
        self.mcp_manager.close()


def _bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager_session_id_override: str | None = None,
) -> tuple[io.StringIO, StdioBridge, str, McpManager]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _configure_mcp(tmp_path, monkeypatch)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(model="test-model", subagents_enabled=False),
    )
    created: list[McpManager] = []

    def create_session(**kwargs: Any) -> _FakeAgentSession:
        session_id = manager_session_id_override or str(kwargs["session_id_override"])
        root = Path(kwargs["root"])
        manager = McpManager(
            resolved_config=load_resolved_mcp_config(workspace_root=root),
            workspace_root=root,
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            session_id=session_id,
        )
        _ = manager.tool_bindings
        created.append(manager)
        return _FakeAgentSession(manager=manager, root=root)

    output = io.StringIO()
    bridge = StdioBridge(
        stdout=output,
        create_session_fn=create_session,
        prompt_queue=DurablePromptQueue(tmp_path / "prompt-queue.sqlite3"),
        managed_browser_factory=lambda **_kwargs: _FakeManagedBrowser(),
    )
    created_response = _send(
        bridge,
        output,
        "session.create",
        {"workspace": os.fspath(tmp_path), "mode": "review", "model": "test-model"},
    )
    assert created_response["ok"] is True
    return output, bridge, created_response["result"]["session_id"], created[0]


def test_live_mcp_server_lifecycle_is_session_owned_trusted_and_cleanup_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, bridge, session_id, manager = _bridge(tmp_path, monkeypatch)
    try:
        status = _send(
            bridge,
            output,
            "mcp.server.status",
            {"session_id": session_id, "server_id": "alpha"},
        )
        assert status["ok"] is True
        assert status["result"]["session_id"] == session_id
        assert status["result"]["connection_state"] == "connected"
        assert status["result"]["secret_values_included"] is False

        untrusted = _send(
            bridge,
            output,
            "mcp.server.disable",
            {"session_id": session_id, "server_id": "alpha"},
        )
        assert untrusted["ok"] is False
        assert untrusted["error"]["code"] == "workspace_trust_required"

        disabled = _send(
            bridge,
            output,
            "mcp.server.disable",
            {
                "session_id": session_id,
                "server_id": "alpha",
                "workspace_trusted": True,
            },
        )
        assert disabled["ok"] is True
        assert disabled["result"]["connection_state"] == "disabled"

        enabled = _send(
            bridge,
            output,
            "mcp.server.enable",
            {
                "session_id": session_id,
                "server_id": "alpha",
                "workspace_trusted": True,
            },
        )
        assert enabled["ok"] is True
        assert enabled["result"]["connection_state"] == "connected"

        restarted = _send(
            bridge,
            output,
            "mcp.server.restart",
            {
                "session_id": session_id,
                "server_id": "alpha",
                "workspace_trusted": True,
            },
        )
        assert restarted["ok"] is True
        assert restarted["result"]["generation"] == 3
    finally:
        bridge.close()
    assert manager.closed is True


def test_live_mcp_lifecycle_rejects_owner_mismatch_and_busy_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, bridge, session_id, _manager = _bridge(
        tmp_path,
        monkeypatch,
        manager_session_id_override="different-owner",
    )
    try:
        mismatch = _send(
            bridge,
            output,
            "mcp.server.status",
            {"session_id": session_id, "server_id": "alpha"},
        )
        assert mismatch["ok"] is False
        assert mismatch["error"]["code"] == "mcp_owner_mismatch"
    finally:
        bridge.close()

    output, bridge, session_id, _manager = _bridge(tmp_path / "busy", monkeypatch)
    try:
        bridge._sessions[session_id].active_job = BridgeJob(
            job_id="busy-job",
            session_id=session_id,
            created_at="2026-07-30T00:00:00Z",
            status="running",
        )
        busy = _send(
            bridge,
            output,
            "mcp.server.disable",
            {
                "session_id": session_id,
                "server_id": "alpha",
                "workspace_trusted": True,
            },
        )
        assert busy["ok"] is False
        assert busy["error"]["code"] == "session_busy"
        assert _manager.server_lifecycle_status(server_id="alpha")["connected"] is True
        bridge._sessions[session_id].active_job = None
    finally:
        bridge.close()


def test_live_mcp_lifecycle_withholds_server_controlled_transport_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, bridge, session_id, manager = _bridge(tmp_path, monkeypatch)
    leaked_marker = "runtime-diagnostic-value-not-pattern-recognizable"

    def fail_restart(*, server_id: str) -> dict[str, Any]:
        assert server_id == "alpha"
        raise McpProcessError(f"server stderr: {leaked_marker}")

    monkeypatch.setattr(manager, "restart_server", fail_restart)
    try:
        response = _send(
            bridge,
            output,
            "mcp.server.restart",
            {
                "session_id": session_id,
                "server_id": "alpha",
                "workspace_trusted": True,
            },
        )
        assert response["ok"] is False
        assert response["error"]["code"] == "mcp_lifecycle_error"
        assert leaked_marker not in json.dumps(response, sort_keys=True)
        assert "diagnostics were withheld" in response["error"]["message"]
    finally:
        bridge.close()
