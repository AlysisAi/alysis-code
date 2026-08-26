from __future__ import annotations

import typer

from alysis_code.cli import app


def test_cli_app_is_exported_and_has_core_commands() -> None:
    assert isinstance(app, typer.Typer)
    callbacks = {getattr(cmd.callback, "__name__", "") for cmd in app.registered_commands}
    assert "run" in callbacks
    assert "chat" in callbacks
