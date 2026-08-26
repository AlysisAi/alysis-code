"""IDE bridge persona surface: session.personas.list / session.persona.set.

Same discipline as test_ide_stdio_bridge.py: a fake agent session behind the
real bridge, real protocol dispatch, and the real chat-loop persona primitive
(`_apply_chat_persona`) with only the tool-surface rebuild faked out, exactly
like test_persona_modes.py exercises it.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import alysis_code.personas as personas_mod
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.stdio_bridge import StdioBridge


def _request(method: str, params: dict[str, Any] | None = None, request_id: str = "req") -> str:
    return json.dumps(
        {
            "protocol_version": "1",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
        sort_keys=True,
    )


def _json_lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def _response_by_id(out: io.StringIO, request_id: str) -> dict[str, Any]:
    return [line for line in _json_lines(out) if line.get("id") == request_id][0]


def _send_bridge_request(
    bridge: StdioBridge,
    out: io.StringIO,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    rid = request_id or method
    bridge.process_line(_request(method, params or {}, request_id=rid) + "\n")
    return _response_by_id(out, rid)


def _events_of_type(out: io.StringIO, event_type: str) -> list[dict[str, Any]]:
    return [line for line in _json_lines(out) if line.get("type") == event_type]


def _wait_for_line(out: io.StringIO, predicate: Any, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in _json_lines(out):
            if predicate(line):
                return line
        time.sleep(0.01)
    raise AssertionError("timed out waiting for protocol line")


def _write_custom_persona(root: Path) -> None:
    directory = root / ".alysis_personas"
    directory.mkdir(exist_ok=True)
    (directory / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "description: Reviews changes before merge\n"
        "exec_mode: readonly\n"
        "model_role: review\n"
        "---\n"
        "Review the diff before proposing edits.\n",
        encoding="utf-8",
    )


def _create_persona_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "review",
    persona_modes_enabled_cfg: bool = True,
    run_turn: Any = None,
) -> tuple[io.StringIO, StdioBridge, str, dict[str, Any]]:
    """A bridge session whose fake agent session carries the chat persona state.

    The real ``_apply_chat_persona`` runs; only the tool-surface rebuild
    (``_apply_chat_effective_mode``) is faked, mirroring test_persona_modes.py.
    """
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    monkeypatch.setattr(personas_mod, "canonical_user_config_dir", lambda: tmp_path / "no-user-dir")
    out = io.StringIO()
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir(exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    def _load_config() -> AppConfig:
        cfg = AppConfig(model="test-model", subagents_enabled=False)
        cfg.persona_modes_enabled = persona_modes_enabled_cfg
        return cfg

    monkeypatch.setattr(stdio_bridge, "load_config", _load_config)

    applied_modes: list[str] = []

    def _fake_apply_mode(*, session: Any, next_mode: str, persist_default_mode: bool) -> None:
        assert persist_default_mode is False
        session.mode = next_mode
        applied_modes.append(next_mode)

    monkeypatch.setattr(chat_loop, "_apply_chat_effective_mode", _fake_apply_mode)

    class FakeStore:
        session_artifact_root = artifact_root

        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def append(self, name: str, payload: dict[str, Any]) -> None:
            self.events.append((name, payload))

    created_sessions: list[Any] = []

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.mode = kwargs["mode"]
            self.surface = kwargs["surface"]
            self.store = FakeStore()
            self.messages: list[dict[str, Any]] = []
            self.persona = "code"
            self.persona_restore_mode: str | None = None
            self.persona_restore_write_globs: list[str] | None = None
            self.allow_write_globs: list[str] | None = None
            self.persona_allow_write_globs: list[str] | None = None

        def run_turn(self, message: str) -> int:
            if run_turn is None:
                return 0
            return int(run_turn(self.surface, message) or 0)

        def close(self) -> None:
            return None

    def _create_session(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created_sessions.append(session)
        return session

    bridge = StdioBridge(stdout=out, create_session_fn=_create_session)
    create = _send_bridge_request(
        bridge,
        out,
        "session.create",
        {"workspace": str(tmp_path), "mode": mode, "model": "test-model"},
        request_id="create",
    )
    assert create["ok"] is True
    session_id = create["result"]["session_id"]
    context = {
        "applied_modes": applied_modes,
        "created_sessions": created_sessions,
    }
    return out, bridge, session_id, context


def test_capabilities_advertise_persona_methods_and_event() -> None:
    payload = capabilities_payload()

    assert "session.personas.list" in payload["methods"]
    assert "session.persona.set" in payload["methods"]
    assert "persona_changed" in payload["events"]


def test_personas_list_returns_builtins_then_customs_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_persona(tmp_path)
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    listed = _send_bridge_request(bridge, out, "session.personas.list", {"session_id": session_id})

    assert listed["ok"] is True
    result = listed["result"]
    assert result["enabled"] is True
    assert result["active"] == "code"
    assert result["active_source"] == "config"
    names = [entry["name"] for entry in result["personas"]]
    assert names == ["code", "architect", "ask", "debug", "reviewer"]
    by_name = {entry["name"]: entry for entry in result["personas"]}
    assert by_name["architect"] == {
        "name": "architect",
        "description": "Planning and design; may write markdown plan documents only.",
        "default_exec_mode": "review",
        "model_role": "planner",
        "source_scope": "builtin",
        "allow_write_globs": ["*.md", "**/*.md"],
    }
    reviewer = by_name["reviewer"]
    assert reviewer["source_scope"] == "custom"
    assert reviewer["default_exec_mode"] == "readonly"
    assert reviewer["model_role"] == "review"
    assert reviewer["description"] == "Reviews changes before merge"
    # Prompt bodies never cross the IDE wire.
    for entry in result["personas"]:
        assert "overlay_prompt" not in entry
        assert "prompt_trust" not in entry


def test_personas_list_reports_disabled_kill_switch_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(
        tmp_path, monkeypatch, persona_modes_enabled_cfg=False
    )

    listed = _send_bridge_request(bridge, out, "session.personas.list", {"session_id": session_id})

    assert listed["ok"] is True
    assert listed["result"]["enabled"] is False
    assert listed["result"]["active"] == "code"
    assert listed["result"]["personas"] == []


def test_personas_list_unknown_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, _session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.personas.list",
        {"session_id": "sess-does-not-exist"},
        request_id="list-missing",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "session_not_found"


def test_persona_set_applies_clamp_and_emits_persona_changed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-architect",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "architect"
    assert response["result"]["effective_mode"] == "review"
    assert response["result"]["model_role"] == "planner"
    assert response["result"]["changed"] is True

    events = _events_of_type(out, "persona_changed")
    assert len(events) == 1
    assert events[0]["payload"] == {
        "persona": "architect",
        "effective_mode": "review",
        "source": "user",
    }
    assert events[0]["session_id"] == session_id

    agent_session = context["created_sessions"][0]
    assert agent_session.persona == "architect"
    assert agent_session.persona_allow_write_globs == ["*.md", "**/*.md"]
    assert agent_session.persona_restore_mode == "review"

    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["persona"] == "architect"
    assert status["result"]["persona_source"] == "user"
    assert status["result"]["mode"] == "review"


def test_persona_set_same_persona_reports_changed_false_without_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "code"},
        request_id="set-same",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "code"
    assert response["result"]["changed"] is False
    assert response["result"]["effective_mode"] == "review"
    assert _events_of_type(out, "persona_changed") == []


def test_persona_set_rejects_unknown_persona_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "wizard"},
        request_id="set-invalid",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_persona"
    assert "code" in response["error"]["message"]
    assert "architect" in response["error"]["message"]
    assert _events_of_type(out, "persona_changed") == []


def test_persona_set_rejected_when_persona_modes_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(
        tmp_path, monkeypatch, persona_modes_enabled_cfg=False
    )

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-disabled",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "persona_modes_disabled"
    assert response["error"]["message"] == "Persona modes are disabled."
    assert _events_of_type(out, "persona_changed") == []


def test_persona_set_rejected_while_session_job_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def _blocking_turn(_surface: Any, _message: str) -> int:
        entered.set()
        assert release.wait(timeout=5.0)
        return 0

    out, bridge, session_id, _context = _create_persona_session(
        tmp_path, monkeypatch, run_turn=_blocking_turn
    )
    bridge.process_line(
        _request("chat.send", {"session_id": session_id, "message": "go"}, request_id="chat") + "\n"
    )
    assert entered.wait(timeout=5.0)
    try:
        response = _send_bridge_request(
            bridge,
            out,
            "session.persona.set",
            {"session_id": session_id, "persona": "architect"},
            request_id="set-busy",
        )
    finally:
        release.set()

    assert response["ok"] is False
    assert response["error"]["code"] == "persona_change_busy"
    assert response["error"]["message"] == "Cannot change persona while a task is running."
    assert _events_of_type(out, "persona_changed") == []

    _wait_for_line(
        out,
        lambda line: (
            line.get("type") == "info_emitted"
            and str(line.get("payload", {}).get("message", "")).startswith("job_completed ")
        ),
    )
    retried = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-after-job",
    )
    assert retried["ok"] is True
    assert retried["result"]["changed"] is True


def test_persona_set_readonly_base_mode_clamps_architect_to_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, context = _create_persona_session(
        tmp_path, monkeypatch, mode="readonly"
    )

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "architect"},
        request_id="set-clamped",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "architect"
    # The clamp lowers, never raises: architect's review default cannot lift a
    # readonly session out of readonly.
    assert response["result"]["effective_mode"] == "readonly"
    events = _events_of_type(out, "persona_changed")
    assert len(events) == 1
    assert events[0]["payload"]["effective_mode"] == "readonly"
    agent_session = context["created_sessions"][0]
    assert agent_session.mode == "readonly"
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["mode"] == "readonly"
    assert status["result"]["persona"] == "architect"


def test_persona_set_supports_custom_personas_from_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_custom_persona(tmp_path)
    out, bridge, session_id, context = _create_persona_session(tmp_path, monkeypatch)

    response = _send_bridge_request(
        bridge,
        out,
        "session.persona.set",
        {"session_id": session_id, "persona": "reviewer"},
        request_id="set-custom",
    )

    assert response["ok"] is True
    assert response["result"]["persona"] == "reviewer"
    assert response["result"]["effective_mode"] == "readonly"
    assert response["result"]["model_role"] == "review"
    assert response["result"]["changed"] is True
    events = _events_of_type(out, "persona_changed")
    assert len(events) == 1
    assert events[0]["payload"] == {
        "persona": "reviewer",
        "effective_mode": "readonly",
        "source": "user",
    }
    agent_session = context["created_sessions"][0]
    assert agent_session.persona == "reviewer"
    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})
    assert status["result"]["persona"] == "reviewer"
    assert status["result"]["persona_source"] == "user"


def test_session_status_reports_persona_defaults_before_any_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, bridge, session_id, _context = _create_persona_session(tmp_path, monkeypatch)

    status = _send_bridge_request(bridge, out, "session.status", {"session_id": session_id})

    assert status["ok"] is True
    assert status["result"]["persona"] == "code"
    assert status["result"]["persona_source"] == "config"
