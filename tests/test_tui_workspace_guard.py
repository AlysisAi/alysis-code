"""Headless tests for the TUI-native guarded-workspace pickers.

Each picker is a short-lived full-screen prompt_toolkit Application, driven here
with a pipe input + dummy output so no real terminal is needed (same harness as
the Phase-1 TUI tests).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.cli_impl.tui.workspace_guard import (
    select_guarded_workspace_action,
    select_workspace_candidate,
    workspace_guard_prompt_text,
)


def _drive(fn, keys: str, **kwargs):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return fn(input=pipe, output=DummyOutput(), **kwargs)


def _binding(path: str = "/home/user"):
    return SimpleNamespace(requested_path=Path(path))


def _candidates(*names: str):
    return tuple(
        SimpleNamespace(path=Path("/home/user") / n, summary=f"{n} project") for n in names
    )


# --------------------------- action picker ---------------------------


def test_action_enter_picks_default_choose_project():
    # Default focus is row 2 ("choose project") when "use current" is offered.
    value, available = _drive(
        select_guarded_workspace_action,
        "\r",
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=True,
    )
    assert available is True
    assert value == "choose_project"


def test_action_digit_one_picks_use_current():
    value, available = _drive(
        select_guarded_workspace_action,
        "1",
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=True,
    )
    assert available is True
    assert value == "use_current"


def test_action_down_then_enter_moves_selection():
    # From default index 1 (choose), Down → index 2 (create folder).
    value, _ = _drive(
        select_guarded_workspace_action,
        "\x1b[B\r",  # Down arrow, then Enter
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=True,
    )
    assert value == "create_folder"


def test_action_ctrl_c_cancels():
    value, available = _drive(
        select_guarded_workspace_action,
        "\x03",
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=True,
    )
    assert available is True
    assert value is None


def test_action_without_use_current_default_is_first_row():
    value, _ = _drive(
        select_guarded_workspace_action,
        "\r",
        binding=_binding(),
        candidates=_candidates("app"),
        allow_use_current_action=False,
    )
    assert value == "choose_project"


# --------------------------- candidate picker ---------------------------


def test_candidate_enter_returns_first_path():
    path, available = _drive(
        select_workspace_candidate,
        "\r",
        base_path=Path("/home/user"),
        candidates=_candidates("alpha", "beta"),
    )
    assert available is True
    assert path == Path("/home/user/alpha")


def test_candidate_digit_two_returns_second_path():
    path, _ = _drive(
        select_workspace_candidate,
        "2",
        base_path=Path("/home/user"),
        candidates=_candidates("alpha", "beta"),
    )
    assert path == Path("/home/user/beta")


def test_candidate_cancel_returns_none():
    path, available = _drive(
        select_workspace_candidate,
        "\x03",
        base_path=Path("/home/user"),
        candidates=_candidates("alpha", "beta"),
    )
    assert available is True
    assert path is None


def test_candidate_empty_is_noninteractive_passthrough():
    # No candidates → nothing to render; defer to the resolver's typed path.
    path, available = select_workspace_candidate(base_path=Path("/home/user"), candidates=())
    assert path is None
    assert available is True


# --------------------------- text prompt ---------------------------


def test_prompt_returns_typed_text():
    assert (
        _drive(workspace_guard_prompt_text, "my-project\r", text="New folder name") == "my-project"
    )


def test_prompt_blank_returns_default():
    assert (
        _drive(workspace_guard_prompt_text, "\r", text="New folder name", default="new-project")
        == "new-project"
    )


def test_prompt_cancel_raises_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        _drive(workspace_guard_prompt_text, "\x03", text="Workspace path")


# --------------------------- wiring into the startup resolver ---------------------------


def _capture_resolver(monkeypatch):
    """Patch the binding impl to capture which selector callbacks it receives."""
    from alysis_code.cli_impl.commands import chat_terminal as ct

    captured: dict = {}

    def fake_impl(**kwargs):
        captured.update(kwargs)
        return "BINDING"

    monkeypatch.setattr(ct, "_resolve_startup_workspace_binding_impl", fake_impl)
    return ct, captured


def test_startup_resolver_uses_tui_selectors_when_enabled(monkeypatch):
    from alysis_code.cli_impl.tui import workspace_guard as wg

    monkeypatch.delenv("ALYSIS_TUI", raising=False)
    ct, captured = _capture_resolver(monkeypatch)
    result = ct._resolve_startup_workspace_binding(
        requested_path=Path("/home/user"), console=None, interactive=True
    )
    assert result == "BINDING"
    assert captured["select_action_interactive"] is wg.select_guarded_workspace_action
    assert captured["select_candidate_interactive"] is wg.select_workspace_candidate
    assert captured["prompt_text"] is wg.workspace_guard_prompt_text


def test_startup_resolver_uses_classic_selectors_when_disabled(monkeypatch):
    from alysis_code.cli_impl.tui import workspace_guard as wg

    monkeypatch.setenv("ALYSIS_TUI", "0")
    ct, captured = _capture_resolver(monkeypatch)
    ct._resolve_startup_workspace_binding(
        requested_path=Path("/home/user"), console=None, interactive=True
    )
    assert captured["select_action_interactive"] is not wg.select_guarded_workspace_action
    assert captured["prompt_text"] is not wg.workspace_guard_prompt_text


def test_startup_resolver_keeps_classic_when_noninteractive(monkeypatch):
    # Non-interactive runs must never spin a TUI even with the flag on.
    from alysis_code.cli_impl.tui import workspace_guard as wg

    monkeypatch.setenv("ALYSIS_TUI", "1")
    ct, captured = _capture_resolver(monkeypatch)
    ct._resolve_startup_workspace_binding(
        requested_path=Path("/home/user"), console=None, interactive=False
    )
    assert captured["select_action_interactive"] is not wg.select_guarded_workspace_action
