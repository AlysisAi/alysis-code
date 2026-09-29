from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli_impl import tui as tui_pkg
from alysis_code.mcp.transport_http import (
    McpHttpTransportAuthRequiredError,
    McpHttpTransportTimeoutError,
)


@pytest.mark.parametrize("surface", ["tui", "classic", "tui_unavailable"])
@pytest.mark.parametrize(
    "error_type", [McpHttpTransportTimeoutError, McpHttpTransportAuthRequiredError]
)
def test_chat_reports_mcp_startup_error_without_retry_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    error_type: type[Exception],
) -> None:
    attempts = 0
    tui_calls = 0
    message = "MCP HTTP server 'vercel': test startup failure [details]"

    def create_session(**_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise error_type(message)

    def run_tui(_state: object, **kwargs: object) -> None:
        nonlocal tui_calls
        tui_calls += 1
        if surface == "tui_unavailable":
            raise RuntimeError("test terminal unavailable")
        session_builder = kwargs["session_builder"]
        assert callable(session_builder)
        session_builder(SimpleNamespace())

    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli_mod, "create_session", create_session)
    monkeypatch.setattr(tui_pkg, "is_tui_enabled", lambda: surface != "classic")
    monkeypatch.setattr(tui_pkg, "run_tui", run_tui)

    result = CliRunner().invoke(
        cli_mod.app,
        [
            "chat",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "test-model",
            "--api-key",
            "test-key",
            "--no-log",
        ],
        env={
            "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
            "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
            "ALYSIS_API_KEY": "",
            "OPENAI_API_KEY": "",
        },
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert attempts == 1
    assert tui_calls == (0 if surface == "classic" else 1)
    assert "MCP error:" in result.output
    assert message in result.output
    assert "Traceback" not in result.output
    assert ("TUI unavailable" in result.output) is (surface == "tui_unavailable")
