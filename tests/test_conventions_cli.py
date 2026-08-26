from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import cli as cli_mod


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_conventions_list_reports_repo_conventions_in_precedence_order(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    focus_dir = workspace / "packages" / "lib" / "src"
    _init_git_repo(workspace)
    focus_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text("Root agent rules.\n", encoding="utf-8")
    (workspace / "packages" / "lib" / "CLAUDE.md").write_text(
        "Nested claude rules.\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        cli_mod.app,
        ["conventions", "list", "--path", str(focus_dir)],
        terminal_width=220,
    )

    assert result.exit_code == 0
    assert "Repo Conventions" in result.output
    assert "packages/lib/CLAUDE.md" in result.output
    assert "AGENTS.md" in result.output
    assert "untrusted" in result.output
    assert result.output.index("CLAUDE.md") < result.output.index("AGENTS.md")


def test_conventions_render_shows_repo_conventions_prompt_block(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    focus_file = workspace / "app" / "main.py"
    _init_git_repo(workspace)
    focus_file.parent.mkdir(parents=True, exist_ok=True)
    focus_file.write_text("print('hello')\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("Follow repository rules.\n", encoding="utf-8")
    (workspace / "app" / "CONVENTIONS.md").write_text(
        "App-specific conventions.\n", encoding="utf-8"
    )

    result = runner.invoke(
        cli_mod.app,
        ["conventions", "render", "--path", str(focus_file), "--max-chars", "4000"],
        terminal_width=220,
    )

    assert result.exit_code == 0
    assert "<repo_conventions>" in result.output
    assert "source: repo-authored conventions files" in result.output
    assert "App-specific conventions." in result.output
    assert "Follow repository rules." in result.output
    assert "[CONVENTIONS.md @" in result.output


def test_conventions_commands_handle_missing_repo_conventions(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    _init_git_repo(workspace)

    list_result = runner.invoke(
        cli_mod.app,
        ["conventions", "list", "--path", str(workspace)],
        terminal_width=220,
    )
    render_result = runner.invoke(
        cli_mod.app,
        ["conventions", "render", "--path", str(workspace)],
        terminal_width=220,
    )

    assert list_result.exit_code == 0
    assert render_result.exit_code == 0
    assert "No repo conventions found." in list_result.output
    assert "No repo conventions found." in render_result.output
