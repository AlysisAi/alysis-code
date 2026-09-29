"""IDE bridge session mutations must run without the interactive CLI facade.

Regression for the sidebar Plan/Act switch and model picker: the chat loop's
mutation primitives resolve ``_rebuild_session_tools_for_mode``,
``refresh_session_environment_context_message`` and
``_refresh_chat_hud_context_cache`` from module globals that the interactive
``alysis chat`` startup injects. The bridge never runs that startup, so
``session.setMode`` / ``session.setModel`` / ``session.setStream`` on an
existing session died with ``NameError`` and the IDE showed "The IDE bridge
encountered an unexpected internal error". The bridge now injects the same
names itself (``_ensure_chat_loop_facade``) before calling into the loop.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.cli_impl.commands import welcome as welcome_mod
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.stdio_bridge import StdioBridge

_FACADE_NAMES = (
    "_rebuild_session_tools_for_mode",
    "refresh_session_environment_context_message",
    "refresh_session_workspace_binding_context_message",
    "_refresh_chat_hud_context_cache",
)


class _FakeSurface:
    def __init__(self) -> None:
        self.modes: list[str] = []

    def emit_mode_changed(self, mode: str) -> None:
        self.modes.append(mode)


class _FakeSession:
    def __init__(self) -> None:
        self.mode = "auto"
        self.surface = _FakeSurface()
        self.store = None


def _strip_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _FACADE_NAMES:
        monkeypatch.delitem(chat_loop.__dict__, name, raising=False)


def test_ensure_chat_loop_facade_injects_every_loop_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _strip_facade(monkeypatch)
    loop = stdio_bridge._ensure_chat_loop_facade()
    assert loop is chat_loop
    for name in _FACADE_NAMES:
        assert callable(chat_loop.__dict__.get(name)), name


def test_apply_agent_session_mode_runs_the_real_loop_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuilt: list[tuple[Any, str]] = []
    refreshed: list[Any] = []

    def _fake_rebuild(*, session: Any, mode: str) -> None:
        rebuilt.append((session, mode))

    monkeypatch.setattr(welcome_mod, "_rebuild_session_tools_for_mode", _fake_rebuild)
    _strip_facade(monkeypatch)
    # The environment-context refresh is one of the injected names too; make sure
    # the bridge wires it rather than letting the loop swallow a NameError.
    monkeypatch.setitem(
        chat_loop.__dict__,
        "refresh_session_environment_context_message",
        lambda session: refreshed.append(session),
    )

    session = _FakeSession()
    stdio_bridge._apply_agent_session_mode(session, "review")

    assert rebuilt == [(session, "review")]
    assert refreshed == [session]
    assert session.mode == "review"
    assert session.surface.modes == ["review"]


def test_ensure_chat_loop_facade_keeps_an_existing_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A facade that already injected the hook must keep its own implementation."""
    facade_calls: list[str] = []
    welcome_calls: list[str] = []

    _strip_facade(monkeypatch)
    monkeypatch.setitem(
        chat_loop.__dict__,
        "_rebuild_session_tools_for_mode",
        lambda *, session, mode: facade_calls.append(mode),
    )
    monkeypatch.setattr(
        welcome_mod,
        "_rebuild_session_tools_for_mode",
        lambda *, session, mode: welcome_calls.append(mode),
    )
    monkeypatch.setitem(
        chat_loop.__dict__, "refresh_session_environment_context_message", lambda session: None
    )

    session = _FakeSession()
    stdio_bridge._apply_agent_session_mode(session, "readonly")

    assert facade_calls == ["readonly"]
    assert welcome_calls == []
    assert session.mode == "readonly"


def test_stdio_bridge_set_mode_and_set_model_survive_without_cli_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the protocol, with the loop's real mutation primitives."""
    _strip_facade(monkeypatch)
    rebuilt: list[str] = []
    monkeypatch.setattr(
        welcome_mod,
        "_rebuild_session_tools_for_mode",
        lambda *, session, mode: rebuilt.append(mode),
    )
    monkeypatch.setattr(
        stdio_bridge, "load_config", lambda: AppConfig(model="default-model", stream=True)
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()

    class FakeStore:
        session_artifact_root = artifact_root

        def append(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    class FakeSession:
        store = FakeStore()

        def __init__(self, **kwargs: Any) -> None:
            self.cfg = kwargs["cfg"]
            self.mode = kwargs["mode"]
            self.surface = kwargs["surface"]
            self.messages: list[dict[str, Any]] = []

        def close(self) -> None:
            pass

    out = io.StringIO()
    bridge = StdioBridge(stdout=out, create_session_fn=lambda **kwargs: FakeSession(**kwargs))

    def _dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
        request = stdio_bridge.ProtocolRequest(
            id=method, method=method, params=params, protocol_version="1"
        )
        result, _ = bridge._dispatch(request)
        return result

    created = _dispatch("session.create", {"workspace": os.fspath(tmp_path), "mode": "auto"})
    session_id = created["session_id"]

    assert (
        _dispatch("session.setMode", {"session_id": session_id, "mode": "review"})["mode"]
        == "review"
    )
    assert (
        _dispatch("session.setModel", {"session_id": session_id, "model": "next-model"})["model"]
        == "next-model"
    )
    assert (
        _dispatch("session.setStream", {"session_id": session_id, "stream": False})["stream"]
        is False
    )
    assert "review" in rebuilt
