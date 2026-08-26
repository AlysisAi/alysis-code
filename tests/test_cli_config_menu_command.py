from __future__ import annotations

from typer.testing import CliRunner

from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl import config_menu as config_menu_mod
from alysis_code.cli_impl.config_menu import ConfigMenuResult


def test_config_menu_command_reports_saved_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        config_menu_mod,
        "run_config_menu",
        lambda: ConfigMenuResult(
            saved=True,
            changes={"model": "gpt-5", "base_url": "https://api.openai.com/v1"},
            api_key_changed=True,
        ),
    )

    result = CliRunner().invoke(alysis_app, ["config", "menu"])

    assert result.exit_code == 0
    assert "Saved 3 change(s)." in result.output


def test_config_menu_command_reports_cancel(monkeypatch) -> None:
    monkeypatch.setattr(
        config_menu_mod,
        "run_config_menu",
        lambda: ConfigMenuResult(saved=False, changes={}, api_key_changed=False),
    )

    result = CliRunner().invoke(alysis_app, ["config", "menu"])

    assert result.exit_code == 0
    assert "Cancelled — no changes saved." in result.output
