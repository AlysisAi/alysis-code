from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from alysis_code import cli as cli_mod
from alysis_code.cli_impl.chat_slash_completer import get_chat_specs
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run, load_plan


def _session(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        cfg=AppConfig(model="fake-model"),
        root=root,
        mode="review",
    )


def _console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(file=stream, force_terminal=False), stream


def test_assets_registered_in_slash_completer() -> None:
    specs = {spec.name: spec for spec in get_chat_specs()}

    assert "assets" in specs
    assert specs["assets"].usage == "/assets"


def test_assets_without_active_run_prints_clear_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    console, stream = _console()
    session = _session(tmp_path)
    monkeypatch.setattr(
        cli_mod,
        "resolve_session_active_workdir_path",
        lambda _session: tmp_path,
    )

    result = cli_mod._handle_chat_command(
        input_text="/assets",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=cli_mod._ForgeChatState(),
        plan_mode_state=cli_mod._ChatPlanModeState(),
    )

    assert result == "handled"
    assert (
        "No forge run is active for this workspace. Use /forge plan to start one."
        in stream.getvalue()
    )


def test_assets_with_active_run_launches_modal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = create_plan_run(tmp_path)
    calls: list[dict[str, object]] = []
    console, _stream = _console()
    session = _session(tmp_path)
    monkeypatch.setattr(
        cli_mod,
        "resolve_session_active_workdir_path",
        lambda _session: tmp_path,
    )

    def fake_modal(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("alysis_code.cli_impl.assets_modal.run_assets_modal", fake_modal)

    result = cli_mod._handle_chat_command(
        input_text="/assets",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=cli_mod._ForgeChatState(),
        plan_mode_state=cli_mod._ChatPlanModeState(),
    )

    assert result == "handled"
    assert calls
    assert calls[0]["run_paths"].run_id == paths.run_id


def test_assets_in_forge_planning_chat_launches_modal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = create_plan_run(tmp_path)
    plan = load_plan(paths)
    calls: list[dict[str, object]] = []
    console, _stream = _console()

    def fake_modal(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("alysis_code.cli_impl.assets_modal.run_assets_modal", fake_modal)

    result = cli_mod._handle_forge_chat_command(
        input_text="/assets",
        forge_state=cli_mod._ForgeChatState(
            ui_mode="forge",
            paths=paths,
            plan=plan,
        ),
        session=_session(tmp_path),
        console=console,
    )

    assert result == "handled"
    assert calls
    assert calls[0]["run_paths"].run_id == paths.run_id
