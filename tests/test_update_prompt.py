"""Tests for the startup update prompt (popup before setup/workspace screens).

Layers, mirroring the existing update + TUI test conventions:
- pure gate logic and prompt-state persistence in ``updates.py``
- the TUI popup driven headlessly (pipe input + dummy output)
- the startup orchestrator in ``cli_impl/commands/update.py``
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer

from alysis_code import cli as cli_mod
from alysis_code.cli_impl.commands import startup as startup_mod
from alysis_code.cli_impl.commands import update as update_cmd_mod
from alysis_code.cli_impl.tui.update_prompt import select_update_action
from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.updates import (
    InstallerPlan,
    UpdateCacheRecord,
    UpdatePromptState,
    UpdateStatus,
    read_update_prompt_state,
    record_update_prompt_shown,
    record_update_skipped,
    resolve_update_prompt_enabled,
    should_prompt_for_update,
    update_prompt_state_path,
    write_update_cache,
    write_update_prompt_state,
)


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **_kwargs) -> None:
        self.lines.append(" ".join(str(arg) for arg in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _status(
    *,
    current: str = "0.9.6",
    latest: str | None = "0.9.8",
    url: str | None = None,
) -> UpdateStatus:
    return UpdateStatus(
        current_version=current,
        latest_version=latest,
        checked_at=datetime.now(UTC),
        source="pypi",
        url=url,
        from_cache=True,
    )


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))
    monkeypatch.delenv("ALYSIS_UPDATE_PROMPT_ENABLED", raising=False)
    monkeypatch.delenv("ALYSIS_UPDATE_CHECK_ENABLED", raising=False)
    monkeypatch.delenv("ALYSIS_UPDATE_CHECK_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("ALYSIS_UPDATE_CACHE_PATH", raising=False)
    monkeypatch.delenv("ALYSIS_UPDATE_PROMPT_STATE_PATH", raising=False)
    monkeypatch.setattr(update_cmd_mod, "_update_prompt_completed", False)
    # Pin the version the orchestrator compares against so the 0.9.8-seeded
    # caches stay "newer" even after the real package version catches up.
    monkeypatch.setattr(update_cmd_mod, "__version__", "0.9.6")


# --------------------------- prompt-state persistence ---------------------------


def test_prompt_state_defaults_when_missing() -> None:
    state = read_update_prompt_state()
    assert state == UpdatePromptState()


def test_prompt_state_roundtrip() -> None:
    now = datetime.now(UTC)
    write_update_prompt_state(
        UpdatePromptState(
            skipped_version="0.9.7",
            last_prompted_version="0.9.8",
            last_prompted_at=now,
        )
    )
    state = read_update_prompt_state()
    assert state.skipped_version == "0.9.7"
    assert state.last_prompted_version == "0.9.8"
    assert state.last_prompted_at == now


def test_prompt_state_rejects_corrupt_and_foreign_payloads() -> None:
    path = update_prompt_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{not json", encoding="utf-8")
    assert read_update_prompt_state() == UpdatePromptState()

    path.write_text(
        json.dumps({"schema_version": 99, "package": "alysis-code"}),
        encoding="utf-8",
    )
    assert read_update_prompt_state() == UpdatePromptState()

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package": "some-other-package",
                "skipped_version": "0.9.8",
            }
        ),
        encoding="utf-8",
    )
    assert read_update_prompt_state() == UpdatePromptState()


def test_record_helpers_preserve_unrelated_fields() -> None:
    record_update_skipped("0.9.7")
    record_update_prompt_shown("0.9.8")
    state = read_update_prompt_state()
    assert state.skipped_version == "0.9.7"
    assert state.last_prompted_version == "0.9.8"
    assert state.last_prompted_at is not None

    record_update_skipped("0.9.8")
    state = read_update_prompt_state()
    assert state.skipped_version == "0.9.8"
    assert state.last_prompted_version == "0.9.8"


# --------------------------- enable knob ---------------------------


def test_update_prompt_enabled_defaults_true() -> None:
    assert AppConfig().update_prompt_enabled is True
    assert resolve_update_prompt_enabled(AppConfig()) is True
    assert resolve_update_prompt_enabled(None) is True


def test_update_prompt_enabled_config_and_env(monkeypatch) -> None:
    cfg = AppConfig(update_prompt_enabled=False)
    assert resolve_update_prompt_enabled(cfg) is False
    monkeypatch.setenv("ALYSIS_UPDATE_PROMPT_ENABLED", "1")
    assert resolve_update_prompt_enabled(cfg) is True
    monkeypatch.setenv("ALYSIS_UPDATE_PROMPT_ENABLED", "0")
    assert resolve_update_prompt_enabled(AppConfig()) is False


def test_update_prompt_enabled_settable_via_config() -> None:
    cfg = AppConfig()
    set_config_value(cfg, "update_prompt_enabled", "false")
    assert cfg.update_prompt_enabled is False
    with pytest.raises(ConfigError):
        set_config_value(cfg, "update_prompt_enabled", "sometimes")


# --------------------------- pure gate ---------------------------


def test_should_prompt_when_update_available_and_clean_state() -> None:
    assert (
        should_prompt_for_update(status=_status(), state=UpdatePromptState(), cfg=AppConfig())
        is True
    )


def test_should_not_prompt_without_newer_release() -> None:
    assert (
        should_prompt_for_update(
            status=_status(latest=None), state=UpdatePromptState(), cfg=AppConfig()
        )
        is False
    )
    assert (
        should_prompt_for_update(
            status=_status(latest="0.9.6"), state=UpdatePromptState(), cfg=AppConfig()
        )
        is False
    )


def test_should_not_prompt_when_disabled() -> None:
    assert (
        should_prompt_for_update(
            status=_status(),
            state=UpdatePromptState(),
            cfg=AppConfig(update_prompt_enabled=False),
        )
        is False
    )
    assert (
        should_prompt_for_update(
            status=_status(),
            state=UpdatePromptState(),
            cfg=AppConfig(update_check_enabled=False),
        )
        is False
    )


def test_should_not_prompt_for_skipped_version_but_prompts_for_newer() -> None:
    state = UpdatePromptState(skipped_version="0.9.8")
    assert should_prompt_for_update(status=_status(), state=state, cfg=AppConfig()) is False
    assert (
        should_prompt_for_update(status=_status(latest="0.9.9"), state=state, cfg=AppConfig())
        is True
    )


def test_should_snooze_recent_prompt_for_same_version_only() -> None:
    now = datetime.now(UTC)
    recent = UpdatePromptState(last_prompted_version="0.9.8", last_prompted_at=now)
    assert (
        should_prompt_for_update(status=_status(), state=recent, cfg=AppConfig(), now=now) is False
    )

    elapsed = UpdatePromptState(
        last_prompted_version="0.9.8",
        last_prompted_at=now - timedelta(hours=25),
    )
    assert (
        should_prompt_for_update(status=_status(), state=elapsed, cfg=AppConfig(), now=now) is True
    )

    other_version = UpdatePromptState(last_prompted_version="0.9.7", last_prompted_at=now)
    assert (
        should_prompt_for_update(status=_status(), state=other_version, cfg=AppConfig(), now=now)
        is True
    )


# --------------------------- TUI popup ---------------------------


def _drive_popup(keys: str, **kwargs):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return select_update_action(
            current_version="0.9.6",
            latest_version="0.9.8",
            input=pipe,
            output=DummyOutput(),
            **kwargs,
        )


def test_popup_enter_defaults_to_later() -> None:
    # Focus starts on "Remind me later" so a reflexive Enter never updates.
    value, available = _drive_popup("\r", command="pipx upgrade alysis-code")
    assert available is True
    assert value == "later"


def test_popup_digit_one_picks_update_now() -> None:
    value, available = _drive_popup("1", command="pipx upgrade alysis-code")
    assert available is True
    assert value == "update"


def test_popup_digit_three_picks_skip() -> None:
    value, available = _drive_popup("3")
    assert available is True
    assert value == "skip"


def test_popup_down_then_enter_moves_selection() -> None:
    value, _ = _drive_popup("\x1b[B\r")  # Down arrow from "later", then Enter
    assert value == "skip"


def test_popup_up_then_enter_picks_update() -> None:
    value, _ = _drive_popup("\x1b[A\r")  # Up arrow from "later", then Enter
    assert value == "update"


def test_popup_ctrl_c_cancels() -> None:
    value, available = _drive_popup("\x03")
    assert available is True
    assert value is None


# --------------------------- startup orchestrator ---------------------------


def _seed_cache(latest: str = "0.9.8") -> None:
    write_update_cache(
        UpdateCacheRecord(
            checked_at=datetime.now(UTC),
            package="alysis-code",
            source="pypi",
            latest_version=latest,
        )
    )


def _supported_plan() -> InstallerPlan:
    return InstallerPlan(
        method="pipx",
        supported=True,
        command=("pipx", "upgrade", "alysis-code"),
        reason="Detected a pipx-managed virtual environment.",
    )


@pytest.fixture()
def _startup_env(monkeypatch):
    monkeypatch.setattr(startup_mod, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli_mod, "detect_installer_plan", _supported_plan, raising=False)
    monkeypatch.setenv("ALYSIS_TUI", "0")
    return monkeypatch


def test_startup_prompt_later_snoozes(_startup_env, monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setattr(update_cmd_mod, "_prompt_update_choice", lambda *a, **k: "later")

    console = _Console()
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=console) == "later"
    state = read_update_prompt_state()
    assert state.last_prompted_version == "0.9.8"
    assert state.skipped_version is None

    # Snoozed: a fresh process (flag reset) still declines to prompt.
    monkeypatch.setattr(update_cmd_mod, "_update_prompt_completed", False)
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=console) is None


def test_startup_prompt_skip_persists_version(_startup_env, monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setattr(update_cmd_mod, "_prompt_update_choice", lambda *a, **k: "skip")

    console = _Console()
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=console) == "skip"
    assert read_update_prompt_state().skipped_version == "0.9.8"
    assert "Skipping version 0.9.8" in console.text


def test_startup_prompt_update_success_exits(_startup_env, monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setattr(update_cmd_mod, "_prompt_update_choice", lambda *a, **k: "update")
    calls: list[InstallerPlan] = []

    def _fake_run(plan: InstallerPlan) -> int:
        calls.append(plan)
        return 0

    monkeypatch.setattr(cli_mod, "run_installer_plan", _fake_run, raising=False)

    console = _Console()
    with pytest.raises(typer.Exit):
        update_cmd_mod.maybe_prompt_update_at_startup(console=console)
    assert len(calls) == 1
    assert "Update installed." in console.text


def test_startup_prompt_update_failure_continues(_startup_env, monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setattr(update_cmd_mod, "_prompt_update_choice", lambda *a, **k: "update")
    monkeypatch.setattr(cli_mod, "run_installer_plan", lambda plan: 1, raising=False)

    console = _Console()
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=console) == "update-failed"
    assert "exit code 1" in console.text


def test_startup_prompt_unsupported_plan_prints_manual_guidance(_startup_env, monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setattr(update_cmd_mod, "_prompt_update_choice", lambda *a, **k: "update")
    monkeypatch.setattr(
        cli_mod,
        "detect_installer_plan",
        lambda: InstallerPlan(method="conda", supported=False, reason="managed by conda"),
        raising=False,
    )

    console = _Console()
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=console) == "manual"
    assert "Update manually" in console.text


def test_startup_prompt_editable_install_is_silent(_startup_env, monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setattr(
        cli_mod,
        "detect_installer_plan",
        lambda: InstallerPlan(method="editable", supported=False, reason="editable install"),
        raising=False,
    )

    # The orchestrator swallows exceptions by design, so record instead of raise.
    prompted: list[str] = []
    monkeypatch.setattr(
        update_cmd_mod,
        "_prompt_update_choice",
        lambda *a, **k: prompted.append("prompted") or "later",
    )

    assert update_cmd_mod.maybe_prompt_update_at_startup(console=_Console()) is None
    assert prompted == []
    assert read_update_prompt_state().last_prompted_version is None


def test_startup_prompt_noop_without_cache(_startup_env, monkeypatch) -> None:
    prompted: list[str] = []
    monkeypatch.setattr(
        update_cmd_mod,
        "_prompt_update_choice",
        lambda *a, **k: prompted.append("prompted") or "later",
    )
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=_Console()) is None
    assert prompted == []


def test_startup_prompt_noop_when_non_interactive(monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setattr(startup_mod, "_is_interactive_terminal", lambda: False)

    prompted: list[str] = []
    monkeypatch.setattr(
        update_cmd_mod,
        "_prompt_update_choice",
        lambda *a, **k: prompted.append("prompted") or "later",
    )
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=_Console()) is None
    assert prompted == []


def test_startup_prompt_noop_when_disabled_via_env(_startup_env, monkeypatch) -> None:
    _seed_cache()
    monkeypatch.setenv("ALYSIS_UPDATE_PROMPT_ENABLED", "0")

    prompted: list[str] = []
    monkeypatch.setattr(
        update_cmd_mod,
        "_prompt_update_choice",
        lambda *a, **k: prompted.append("prompted") or "later",
    )
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=_Console()) is None
    assert prompted == []


def test_startup_prompt_runs_once_per_process(_startup_env, monkeypatch) -> None:
    _seed_cache()
    seen: list[str] = []

    def _choice(*_args, **_kwargs) -> str:
        seen.append("prompted")
        return "later"

    monkeypatch.setattr(update_cmd_mod, "_prompt_update_choice", _choice)

    console = _Console()
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=console) == "later"
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=console) is None
    assert seen == ["prompted"]


# --------------------------- choice dispatch (TUI vs classic) ---------------------------


def test_choice_dispatch_uses_tui_popup_when_enabled(monkeypatch) -> None:
    from alysis_code.cli_impl.tui import update_prompt as popup_mod

    monkeypatch.delenv("ALYSIS_TUI", raising=False)
    captured: dict[str, object] = {}

    def _fake_select(**kwargs):
        captured.update(kwargs)
        return "update", True

    monkeypatch.setattr(popup_mod, "select_update_action", _fake_select)

    console = _Console()
    choice = update_cmd_mod._prompt_update_choice(console, _status(), _supported_plan())
    assert choice == "update"
    assert captured["latest_version"] == "0.9.8"
    assert captured["command"] == "pipx upgrade alysis-code"
    assert captured["unsupported_reason"] is None
    assert console.lines == []  # no classic output when the popup handled it


def test_choice_dispatch_treats_popup_cancel_as_later(monkeypatch) -> None:
    from alysis_code.cli_impl.tui import update_prompt as popup_mod

    monkeypatch.delenv("ALYSIS_TUI", raising=False)
    monkeypatch.setattr(popup_mod, "select_update_action", lambda **_k: (None, True))

    choice = update_cmd_mod._prompt_update_choice(_Console(), _status(), _supported_plan())
    assert choice == "later"


def test_choice_dispatch_falls_back_to_classic_when_popup_unavailable(monkeypatch) -> None:
    from alysis_code.cli_impl.tui import update_prompt as popup_mod

    monkeypatch.delenv("ALYSIS_TUI", raising=False)
    monkeypatch.setattr(popup_mod, "select_update_action", lambda **_k: (None, False))
    monkeypatch.setattr(typer, "prompt", lambda *_a, **_k: "s")

    console = _Console()
    choice = update_cmd_mod._prompt_update_choice(console, _status(), _supported_plan())
    assert choice == "skip"
    assert "0.9.8 is available" in console.text


def test_choice_dispatch_passes_unsupported_reason(monkeypatch) -> None:
    from alysis_code.cli_impl.tui import update_prompt as popup_mod

    monkeypatch.delenv("ALYSIS_TUI", raising=False)
    captured: dict[str, object] = {}

    def _fake_select(**kwargs):
        captured.update(kwargs)
        return "later", True

    monkeypatch.setattr(popup_mod, "select_update_action", _fake_select)

    plan = InstallerPlan(method="conda", supported=False, reason="managed by conda")
    assert update_cmd_mod._prompt_update_choice(_Console(), _status(), plan) == "later"
    assert captured["command"] is None
    assert captured["unsupported_reason"] == "managed by conda"


# --------------------------- classic fallback ---------------------------


def test_classic_prompt_maps_answers(monkeypatch) -> None:
    console = _Console()
    plan = _supported_plan()
    status = _status(url="https://pypi.org/project/alysis-code/")

    for answer, expected in (("y", "update"), ("s", "skip"), ("n", "later"), ("", "later")):
        monkeypatch.setattr(typer, "prompt", lambda *_a, _answer=answer, **_k: _answer or "n")
        assert update_cmd_mod._classic_update_prompt(console, status, plan) == expected

    # Real typer.prompt surfaces Ctrl-C/Ctrl-D as typer.Abort; also keep the
    # raw KeyboardInterrupt case for non-click prompt shims.
    for exc in (typer.Abort, KeyboardInterrupt):

        def _interrupt(*_args, _exc=exc, **_kwargs):
            raise _exc

        monkeypatch.setattr(typer, "prompt", _interrupt)
        assert update_cmd_mod._classic_update_prompt(console, status, plan) == "later"
    assert "0.9.8 is available" in console.text
    assert "pipx upgrade alysis-code" in console.text


# --------------------------- config round-trip ---------------------------


def test_update_prompt_enabled_round_trips_through_saved_config() -> None:
    from alysis_code.config import load_config, save_config

    cfg = AppConfig()
    set_config_value(cfg, "update_prompt_enabled", "false")
    save_config(cfg)
    assert load_config().update_prompt_enabled is False


# --------------------------- stale cache still prompts ---------------------------


def test_startup_prompt_fires_from_stale_cache(_startup_env, monkeypatch) -> None:
    # The cache-only design must offer the update even when the cached check
    # is older than the freshness interval (the background refresh catches up
    # separately); staleness only suppresses records with no latest_version.
    write_update_cache(
        UpdateCacheRecord(
            checked_at=datetime.now(UTC) - timedelta(hours=48),
            package="alysis-code",
            source="pypi",
            latest_version="0.9.8",
        )
    )
    monkeypatch.setattr(update_cmd_mod, "_prompt_update_choice", lambda *a, **k: "later")
    assert update_cmd_mod.maybe_prompt_update_at_startup(console=_Console()) == "later"


# --------------------------- launch wiring (root callback) ---------------------------


def _root_launch_env(monkeypatch) -> list[str]:
    """Patch the bare-launch collaborators and return the call-order recorder."""
    order: list[str] = []
    monkeypatch.setattr(cli_mod, "_is_interactive_terminal", lambda: True, raising=False)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False, raising=False)
    monkeypatch.setattr(startup_mod, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli_mod, "detect_installer_plan", _supported_plan, raising=False)
    monkeypatch.setenv("ALYSIS_TUI", "0")
    monkeypatch.delenv("ALYSIS_HOME_PROMPT", raising=False)

    def _wizard() -> bool:
        order.append("wizard")
        return False  # abort launch after the wizard so chat never starts

    monkeypatch.setattr(cli_mod, "_maybe_run_first_run_setup_wizard", _wizard, raising=False)
    return order


def test_bare_launch_prompts_before_setup_wizard(monkeypatch) -> None:
    from typer.testing import CliRunner

    from alysis_code.cli import app as alysis_app

    _seed_cache()
    order = _root_launch_env(monkeypatch)
    monkeypatch.setattr(
        update_cmd_mod,
        "_prompt_update_choice",
        lambda *a, **k: order.append("prompt") or "later",
    )

    result = CliRunner().invoke(alysis_app, [])
    assert result.exit_code == 0, result.output
    assert order == ["prompt", "wizard"]


def test_bare_launch_update_success_exits_before_setup_wizard(monkeypatch) -> None:
    from typer.testing import CliRunner

    from alysis_code.cli import app as alysis_app

    _seed_cache()
    order = _root_launch_env(monkeypatch)
    monkeypatch.setattr(
        update_cmd_mod,
        "_prompt_update_choice",
        lambda *a, **k: order.append("prompt") or "update",
    )
    monkeypatch.setattr(cli_mod, "run_installer_plan", lambda plan: 0, raising=False)

    result = CliRunner().invoke(alysis_app, [])
    assert result.exit_code == 0, result.output
    assert order == ["prompt"]  # process exits for restart; wizard never runs
    assert "Update installed." in result.output
