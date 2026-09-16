from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code import cli as cli_mod
from alysis_code.agent.steering import ResolvedOperation
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.cli_impl.tui.footer import footer_fragments
from alysis_code.cli_impl.tui.state import TuiState


class _Surface:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[str] = []
        self.fail = fail

    def emit_mode_changed(self, mode: str) -> None:
        if self.fail:
            raise RuntimeError("surface projection failed")
        self.events.append(mode)


class _Store:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


def _session(*, mode: str = "review") -> SimpleNamespace:
    return SimpleNamespace(
        mode=mode,
        pending_permissions_mode=None,
        cfg=SimpleNamespace(default_mode=mode, toolbar_items=["mode"]),
        surface=_Surface(),
        store=_Store(),
        allow_write_globs=None,
        persona_allow_write_globs=None,
        persona_restore_mode=None,
        persona_restore_write_globs=None,
        client=SimpleNamespace(model="test-model", temperature=0.2),
        stream=True,
        subagents_enabled=False,
        usage_summary=None,
    )


def _plain(fragments: Any) -> str:
    return "".join(text for _style, text in fragments)


def test_permissions_selection_is_latest_wins_and_does_not_activate() -> None:
    session = _session()

    assert chat_loop._stage_chat_permissions(session=session, next_mode="auto") == "auto"
    assert session.mode == "review"
    assert session.cfg.default_mode == "review"
    assert session.surface.events == []

    assert (
        chat_loop._stage_chat_permissions(session=session, next_mode="fullaccess") == "fullaccess"
    )
    assert session.pending_permissions_mode == "fullaccess"

    # Selecting the active mode is a deliberate cancellation of the pending one.
    assert chat_loop._stage_chat_permissions(session=session, next_mode="review") is None
    assert session.pending_permissions_mode is None


def test_selecting_persona_narrowed_mode_still_redefines_base_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(mode="readonly")
    session.cfg.default_mode = "review"
    session.persona_restore_mode = "review"
    session.persona_restore_write_globs = ["src/**"]
    session.persona_allow_write_globs = ["docs/**"]
    applied: list[tuple[str, bool]] = []

    def apply_mode(*, session: Any, next_mode: str, persist_default_mode: bool) -> None:
        applied.append((next_mode, persist_default_mode))
        session.mode = next_mode
        if persist_default_mode:
            session.cfg.default_mode = next_mode

    monkeypatch.setattr(chat_loop, "_apply_chat_effective_mode", apply_mode)

    assert chat_loop._stage_chat_permissions(session=session, next_mode="readonly") == "readonly"
    assert chat_loop._activate_pending_chat_permissions(session=session) == "readonly"

    assert applied == [("readonly", True)]
    assert session.cfg.default_mode == "readonly"
    assert session.pending_permissions_mode is None
    assert session.allow_write_globs == ["src/**"]
    assert session.persona_allow_write_globs is None
    assert session.persona_restore_mode is None


def test_tui_permission_operation_updates_only_pending_state() -> None:
    session = _session()
    state = TuiState(model_name="test-model", exec_mode="review")

    chat_loop._apply_tui_step_operation(
        session=session,
        operation=ResolvedOperation("mode", "auto", "permissions: fast"),
        tui_state=state,
    )

    assert session.mode == "review"
    assert session.cfg.default_mode == "review"
    assert session.pending_permissions_mode == "auto"
    assert state.exec_mode == "review"
    assert state.pending_exec_mode == "auto"
    assert session.surface.events == []


def test_permissions_activation_uses_normal_mode_path_and_emits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    applied: list[tuple[str, bool]] = []

    def apply_mode(*, session: Any, next_mode: str, persist_default_mode: bool) -> None:
        applied.append((next_mode, persist_default_mode))
        session.mode = next_mode
        if persist_default_mode:
            session.cfg.default_mode = next_mode
        session.surface.emit_mode_changed(next_mode)

    monkeypatch.setattr(chat_loop, "_apply_chat_effective_mode", apply_mode)

    chat_loop._stage_chat_permissions(session=session, next_mode="auto")
    assert session.surface.events == []

    assert chat_loop._activate_pending_chat_permissions(session=session) == "auto"
    assert session.mode == "auto"
    assert session.pending_permissions_mode is None
    assert session.cfg.default_mode == "auto"
    assert applied == [("auto", True)]
    assert session.surface.events == ["auto"]

    assert chat_loop._activate_pending_chat_permissions(session=session) is None
    assert session.surface.events == ["auto"]


def test_context_projection_failure_does_not_rollback_committed_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.tools = {"old": object()}

    def publish_tools(*, session: Any, mode: str) -> None:
        assert mode == "auto"
        session.tools = {"new": object()}

    def fail_refresh(_session: Any) -> None:
        raise RuntimeError("context projection failed")

    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        publish_tools,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        fail_refresh,
        raising=False,
    )

    chat_loop._stage_chat_permissions(session=session, next_mode="auto")
    assert chat_loop._activate_pending_chat_permissions(session=session) == "auto"

    assert session.mode == "auto"
    assert session.cfg.default_mode == "auto"
    assert set(session.tools) == {"new"}
    assert session.pending_permissions_mode is None
    assert session.surface.events == ["auto"]
    assert session.store.events[0][1]["warning"] == "mode_context_refresh_failed"


def test_surface_projection_failure_does_not_rollback_committed_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.surface = _Surface(fail=True)

    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        lambda **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        chat_loop,
        "refresh_session_environment_context_message",
        lambda _session: None,
        raising=False,
    )

    chat_loop._stage_chat_permissions(session=session, next_mode="auto")
    assert chat_loop._activate_pending_chat_permissions(session=session) == "auto"

    assert session.mode == "auto"
    assert session.cfg.default_mode == "auto"
    assert session.pending_permissions_mode is None
    assert session.store.events[0][1]["warning"] == "mode_surface_emit_failed"


def test_permissions_activation_failure_retains_selection_and_persona_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.allow_write_globs = ["*.md"]
    session.persona_allow_write_globs = ["docs/**"]
    session.persona_restore_mode = "review"
    session.persona_restore_write_globs = ["*.py"]
    chat_loop._stage_chat_permissions(session=session, next_mode="auto")

    def fail_rebuild(**_kwargs: Any) -> None:
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(
        chat_loop,
        "_rebuild_session_tools_for_mode",
        fail_rebuild,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="rebuild failed"):
        chat_loop._activate_pending_chat_permissions(session=session)

    assert session.mode == "review"
    assert session.pending_permissions_mode == "auto"
    assert session.cfg.default_mode == "review"
    assert session.allow_write_globs == ["*.md"]
    assert session.persona_allow_write_globs == ["docs/**"]
    assert session.persona_restore_mode == "review"
    assert session.persona_restore_write_globs == ["*.py"]


def test_pending_permissions_are_distinct_in_tui_and_classic_status() -> None:
    session = _session()
    session.pending_permissions_mode = "auto"
    state = TuiState(model_name="test-model", exec_mode="review")

    chat_loop._sync_tui_session_state(state, session, include_exec_mode=True)

    assert state.exec_mode == "review"
    assert state.pending_exec_mode == "auto"
    assert "safe→fast next" in _plain(footer_fragments(state, width=120))
    assert "review→auto next" in cli_mod._chat_bottom_toolbar(
        session=session,
        pending_images=[],
    )

    spec = cli_mod._chat_status_panel_spec(session=session, pending_images=[])
    rows = dict(
        (name, value)
        for title, section_rows in spec["sections"]
        if title == "Session"
        for name, value, _tone in section_rows
    )
    assert "safe (review)" in rows["mode"]
    assert "fast (auto) next" in rows["mode"]


def test_persona_base_redefinition_is_visible_when_pending_matches_active() -> None:
    session = _session(mode="readonly")
    session.persona = "ask"
    session.persona_restore_mode = "review"
    session.pending_permissions_mode = "readonly"
    state = TuiState(model_name="test-model", exec_mode="readonly", persona="ask")

    chat_loop._sync_tui_session_state(state, session, include_exec_mode=True)

    assert "ask · read (next base)" in _plain(footer_fragments(state, width=120))
    assert "readonly (next base)" in cli_mod._chat_bottom_toolbar(
        session=session,
        pending_images=[],
    )

    spec = cli_mod._chat_status_panel_spec(session=session, pending_images=[])
    rows = dict(
        (name, value)
        for title, section_rows in spec["sections"]
        if title == "Session"
        for name, value, _tone in section_rows
    )
    assert rows["mode"] == "read (readonly) (selected as next base)"
