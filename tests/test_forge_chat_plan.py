from __future__ import annotations

import io
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import agent_loop as agent_loop_mod
from alysis_code import cli as cli_mod
from alysis_code import forge as forge_mod
from alysis_code import workspace_binding as workspace_binding_mod
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl import chat as chat_impl_mod
from alysis_code.cli_impl.chat import commands as chat_commands_mod
from alysis_code.config import AppConfig
from alysis_code.forge import (
    add_task,
    create_plan_run,
    load_current_run_paths,
    load_plan,
    save_plan,
    write_current_run_pointer,
)
from alysis_code.llm.openai_compat import LLMError
from alysis_code.run_lock import write_run_mutation_lock_metadata
from alysis_code.session_store import read_session_events
from alysis_code.swarm_trace import build_swarm_trace_event
from alysis_code.usage_tracker import UsageSummary


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }


def _init_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", os.fspath(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _forge_run_paths(
    tmp_path: Path,
    *,
    run_id: str = "wf",
    root: Path | None = None,
) -> SimpleNamespace:
    workspace_root = (root or tmp_path).resolve()
    runtime_dir = tmp_path / ".alysis"
    runs_dir = runtime_dir / "runs"
    run_dir = tmp_path / ".alysis" / "runs" / run_id
    plan_dir = run_dir / "plan"
    notes_dir = plan_dir / "notes"
    context_dir = plan_dir / "context"
    execution_dir = run_dir / "execution"
    asset_store_dir = run_dir / "assets"
    notes_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)
    execution_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        run_id=run_id,
        root=workspace_root,
        runtime_dir=runtime_dir,
        runs_dir=runs_dir,
        run_dir=run_dir,
        plan_dir=plan_dir,
        plan_json_path=plan_dir / "plan.json",
        plan_md_path=plan_dir / "PLAN.md",
        decisions_path=plan_dir / "DECISIONS.md",
        risks_path=plan_dir / "RISKS.md",
        assets_dir=plan_dir / "assets",
        assets_text_dir=plan_dir / "assets_text",
        asset_store_dir=asset_store_dir,
        assets_index_path=asset_store_dir / "index.json",
        assets_index_lock_path=asset_store_dir / "index.lock",
        assets_raw_dir=asset_store_dir / "raw",
        assets_extracted_dir=asset_store_dir / "extracted",
        assets_comprehensions_dir=asset_store_dir / "comprehensions",
        notes_dir=notes_dir,
        notes_path=notes_dir / "user_notes.md",
        planner_chat_path=notes_dir / "planner_chat.md",
        planner_summary_path=notes_dir / "planner_summary.md",
        plan_replans_dir=plan_dir / "replans",
        execution_dir=execution_dir,
        execution_reports_dir=execution_dir / "reports",
        execution_patches_dir=execution_dir / "patches",
        execution_logs_dir=execution_dir / "logs",
        execution_sessions_dir=execution_dir / "sessions",
        execution_reviews_dir=execution_dir / "reviews",
        execution_verify_dir=execution_dir / "verify",
        execution_context_dir=execution_dir / "context",
        execution_budgets_dir=execution_dir / "budgets",
        execution_asset_briefings_dir=execution_dir / "asset_briefings",
        execution_asset_usage_dir=execution_dir / "asset_usage",
        execution_knowledge_capture_dir=execution_dir / "knowledge_capture",
        execution_integration_dir=execution_dir / "integration",
        execution_integration_issues_path=execution_dir / "integration" / "integration_issues.md",
        knowledge_dir=run_dir / "knowledge",
        knowledge_index_path=run_dir / "knowledge" / "index.json",
        knowledge_task_attempts_dir=run_dir / "knowledge" / "task_attempts",
        knowledge_issues_dir=run_dir / "knowledge" / "issues",
        knowledge_facts_dir=run_dir / "knowledge" / "facts",
        knowledge_decisions_dir=run_dir / "knowledge" / "decisions",
        knowledge_selected_dir=run_dir / "knowledge" / "selected",
        plan_context_dir=context_dir,
        workspace_context_json_path=context_dir / "workspace_context.json",
        workspace_summary_md_path=context_dir / "workspace_summary.md",
        focus_path=workspace_root,
        focus_relpath=".",
        workspace_kind="plain_dir",
        git_root=None,
        has_head_commit=False,
        current_branch=None,
        binding_requested_path=workspace_root,
        binding_source="explicit_path",
        workspace_created_at_startup=False,
        binding_risk_level="healthy",
        binding_risk_reasons=(),
        binding_broad_workspace_override_used=False,
    )


def _install_dummy_forge_entry(monkeypatch, tmp_path: Path) -> None:
    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
        notes_dir = plan_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        forge_state.ui_mode = "forge"
        forge_state.paths = SimpleNamespace(
            run_id="wf",
            plan_json_path=plan_dir / "plan.json",
            plan_md_path=plan_dir / "PLAN.md",
            notes_dir=notes_dir,
            notes_path=notes_dir / "user_notes.md",
        )
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "x",
            "summary": "x",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)


def _assert_forge_plan_command_guidance(rendered: str) -> None:
    assert "Usage:" in rendered
    assert "/plan tasks|table|markdown|md|edit" in rendered
    assert "/plan markdown" in rendered
    assert "/plan edit" in rendered
    assert "/plan <task>" not in rendered


@pytest.mark.parametrize(
    "command",
    ["/plan", "/plan implement feature", "/plan mode", "/plan approve", "/plan off"],
)
def test_plan_is_not_a_chat_command_outside_forge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    # Chat-side plan mode (the draft -> approve -> execute flow and the older
    # readonly overlay) is gone. Outside Forge, /plan is simply not a command:
    # no draft, no model call, no mode change, and no pointer text of its own.
    runner = CliRunner()
    captured: dict[str, Any] = {"run_turn_calls": 0, "rebuild_modes": []}

    class _DummyStore:
        session_id = "sid"

    class _DummyClient:
        model = "test-model"
        temperature = 1.0

        @staticmethod
        def chat(**_kwargs: Any) -> Any:
            raise AssertionError("/plan in chat must not call the model")

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="test-model", default_mode="review")

        def run_turn(self, instruction: str, **_kwargs: Any) -> int:
            captured["run_turn_calls"] += 1
            captured["instruction"] = instruction
            return 0

        @staticmethod
        def close() -> None:
            return None

    session = _DummySession()
    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: session)
    monkeypatch.setattr(
        cli_mod,
        "_rebuild_session_tools_for_mode",
        lambda *, session, mode: captured["rebuild_modes"].append(mode),
    )

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input=f"{command}\nhello\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "Unknown command: /plan" in result.output
    assert "Plan (draft)" not in result.output
    assert "Plan Mode" not in result.output
    assert "/plan <task>" not in result.output
    assert captured["rebuild_modes"] == []
    assert session.mode == "review"
    # The next plain message still runs as a normal turn.
    assert captured["run_turn_calls"] == 1
    assert captured["instruction"] == "hello"


def test_chat_surfaces_do_not_advertise_plan_outside_forge() -> None:
    from alysis_code.cli_impl.chat.forge_visibility import command_visible
    from alysis_code.cli_impl.chat_slash_completer import ChatSlashCompleter, get_chat_specs

    assert not command_visible("/plan", ui_mode="chat")
    assert command_visible("/plan", ui_mode="forge")
    assert "plan" not in {spec.name for spec in get_chat_specs()}

    chat_completer = ChatSlashCompleter(mode_provider=lambda: "chat")
    chat_entries = [entry.completion for entry in chat_completer._matching_completions("/pl")]
    assert "/plan" not in chat_entries

    forge_completer = ChatSlashCompleter(mode_provider=lambda: "forge")
    forge_entries = [entry.completion for entry in forge_completer._matching_completions("/plan ")]
    assert {"/plan tasks", "/plan markdown", "/plan md", "/plan edit"} <= set(forge_entries)

    help_text = "\n".join(
        f"{cmd} {desc}"
        for _section, rows in cli_mod._chat_command_sections(ui_mode="chat")
        for cmd, desc in rows
    )
    assert "/plan" not in help_text
    assert "/plan" not in cli_mod._chat_visible_commands(ui_mode="chat")
    assert "/plan" in cli_mod._chat_visible_commands(ui_mode="forge")


def _assert_forge_help_mentions_plan_views(rendered: str) -> None:
    assert "/plan markdown|md" in rendered
    assert "/plan edit" in rendered
    assert "/plan tasks|markdown|edit views or edits the plan" in rendered
    assert "/plan <task>" not in rendered


def test_back_outside_forge_uses_unknown_command_path(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"
        cfg = AppConfig(model="test-model", default_mode="review")

        def run_turn(self, *_args: Any, **_kwargs: Any) -> int:
            raise AssertionError("/back should be handled as a command")

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Unknown command: /back" in result.output
    assert "Already in chat." not in result.output


def test_forge_entry_refreshes_workspace_context_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    existing_paths = create_plan_run(repo)
    assert existing_paths.workspace_context_json_path is not None
    assert existing_paths.workspace_summary_md_path is not None
    existing_paths.workspace_context_json_path.unlink()
    existing_paths.workspace_summary_md_path.unlink()

    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)
    forge_state = cli_mod._ForgeChatState()
    console = Console(file=io.StringIO(), force_terminal=False)

    ok = cli_mod._enter_forge_mode(root=repo, console=console, forge_state=forge_state)

    assert ok is True
    assert forge_state.paths is not None
    assert forge_state.paths.run_id != existing_paths.run_id
    assert forge_state.paths.workspace_context_json_path is not None
    assert forge_state.paths.workspace_summary_md_path is not None
    assert forge_state.paths.workspace_context_json_path.exists()
    assert forge_state.paths.workspace_summary_md_path.exists()


def test_forge_entry_prompts_for_planner_only_after_workspace_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    prompted: dict[str, int] = {"count": 0}

    def fake_prompt(*, console: Console) -> bool:
        _ = console
        prompted["count"] += 1
        return False

    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", fake_prompt)
    forge_state = cli_mod._ForgeChatState()
    console = Console(file=io.StringIO(), force_terminal=False)

    ok = cli_mod._enter_forge_mode(root=repo, console=console, forge_state=forge_state)

    assert ok is True
    assert prompted["count"] == 1


def test_forge_entry_uses_compact_monochrome_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)
    forge_state = cli_mod._ForgeChatState()
    console_file = io.StringIO()
    console = Console(file=console_file, force_terminal=False, width=140)

    ok = cli_mod._enter_forge_mode(root=repo, console=console, forge_state=forge_state)

    assert ok is True
    rendered = console_file.getvalue()
    assert "Forge ready" in rendered
    assert "assistant off" in rendered
    assert "/show for summary" in rendered
    assert "/execute plan when ready" in rendered
    assert "Engineer Team" not in rendered
    assert "Commands:" not in rendered
    assert "Planner assistant: OFF" not in rendered
    assert "type freely to add requirements" not in rendered


def test_forge_entry_blocks_guarded_workspace_before_planner_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    prompted: dict[str, int] = {"count": 0}

    def fake_prompt(*, console: Console) -> bool:
        _ = console
        prompted["count"] += 1
        return True

    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", fake_prompt)
    forge_state = cli_mod._ForgeChatState()
    console_file = io.StringIO()
    console = Console(file=console_file, force_terminal=False)

    ok = cli_mod._enter_forge_mode(root=home, console=console, forge_state=forge_state)

    assert ok is False
    assert prompted["count"] == 0
    rendered = console_file.getvalue()
    assert "Forge requires a narrower workspace before planning." in rendered
    assert "Forge requires a narrower workspace" in rendered


def test_chat_forge_plain_entry_creates_fresh_run_even_when_current_pointer_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)

    first_chat = cli_mod._ForgeChatState()
    first_console = Console(file=io.StringIO(), force_terminal=False)
    assert cli_mod._enter_forge_mode(root=repo, console=first_console, forge_state=first_chat)
    first_run_id = first_chat.paths.run_id if first_chat.paths is not None else ""
    assert first_run_id
    assert load_current_run_paths(repo).run_id == first_run_id

    second_chat = cli_mod._ForgeChatState()
    second_console = Console(file=io.StringIO(), force_terminal=False)
    assert cli_mod._enter_forge_mode(root=repo, console=second_console, forge_state=second_chat)
    second_run_id = second_chat.paths.run_id if second_chat.paths is not None else ""

    assert second_run_id
    assert second_run_id != first_run_id
    assert load_current_run_paths(repo).run_id == second_run_id


def test_chat_forge_resume_explicitly_uses_current_run_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pointer_paths = create_plan_run(repo)
    session_paths = create_plan_run(repo)
    write_current_run_pointer(pointer_paths)
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)

    console_file = io.StringIO()
    forge_state = cli_mod._ForgeChatState(
        paths=session_paths,
        plan=load_plan(session_paths),
        entry_request_mode="resume_pointer",
    )
    console = Console(file=console_file, force_terminal=False)

    assert cli_mod._enter_forge_mode(root=repo, console=console, forge_state=forge_state)
    assert forge_state.paths is not None
    assert forge_state.paths.run_id == pointer_paths.run_id
    assert load_current_run_paths(repo).run_id == pointer_paths.run_id
    assert "Resumed the current run pointer explicitly." in console_file.getvalue()


def test_chat_forge_reenter_after_back_resumes_session_local_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)

    forge_state = cli_mod._ForgeChatState()
    first_console = Console(file=io.StringIO(), force_terminal=False)
    assert cli_mod._enter_forge_mode(root=repo, console=first_console, forge_state=forge_state)
    original_run_id = forge_state.paths.run_id if forge_state.paths is not None else ""
    assert original_run_id

    back_console = Console(file=io.StringIO(), force_terminal=False)
    back_result = chat_impl_mod._handle_forge_chat_command_impl(
        cli_mod,
        input_text="/back",
        forge_state=forge_state,
        session=SimpleNamespace(),
        console=back_console,
    )

    assert back_result == "handled"
    assert forge_state.ui_mode == "chat"

    second_console_file = io.StringIO()
    second_console = Console(file=second_console_file, force_terminal=False)
    assert cli_mod._enter_forge_mode(root=repo, console=second_console, forge_state=forge_state)
    assert forge_state.paths is not None
    assert forge_state.paths.run_id == original_run_id
    assert (
        "Resumed this chat session's Forge run in the current workspace."
        in second_console_file.getvalue()
    )


def test_chat_forge_plain_entry_does_not_resume_session_local_run_across_workspace_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)

    forge_state = cli_mod._ForgeChatState()
    first_console = Console(file=io.StringIO(), force_terminal=False)
    assert cli_mod._enter_forge_mode(
        root=repo_a,
        console=first_console,
        forge_state=forge_state,
    )
    original_run_dir = forge_state.paths.run_dir if forge_state.paths is not None else None
    assert original_run_dir is not None

    back_result = chat_impl_mod._handle_forge_chat_command_impl(
        cli_mod,
        input_text="/back",
        forge_state=forge_state,
        session=SimpleNamespace(),
        console=Console(file=io.StringIO(), force_terminal=False),
    )
    assert back_result == "handled"

    second_console_file = io.StringIO()
    second_console = Console(file=second_console_file, force_terminal=False)
    assert cli_mod._enter_forge_mode(
        root=repo_b,
        console=second_console,
        forge_state=forge_state,
    )
    assert forge_state.paths is not None
    assert forge_state.paths.root == repo_b.resolve()
    # Counter-based run ids are workspace-scoped, so two workspaces may share an
    # id like "001-vulcan"; the run directory is what must differ.
    assert forge_state.paths.run_dir != original_run_dir
    assert (
        "Started a fresh Forge run because this chat moved to a different workspace."
        in second_console_file.getvalue()
    )


def test_chat_forge_plain_entry_still_resumes_session_local_run_within_same_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    focus_a = repo / "packages" / "a"
    focus_b = repo / "packages" / "b"
    focus_a.mkdir(parents=True)
    focus_b.mkdir(parents=True)
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)

    forge_state = cli_mod._ForgeChatState()
    first_console = Console(file=io.StringIO(), force_terminal=False)
    assert cli_mod._enter_forge_mode(
        root=focus_a,
        console=first_console,
        forge_state=forge_state,
    )
    original_run_id = forge_state.paths.run_id if forge_state.paths is not None else ""
    assert original_run_id

    back_result = chat_impl_mod._handle_forge_chat_command_impl(
        cli_mod,
        input_text="/back",
        forge_state=forge_state,
        session=SimpleNamespace(),
        console=Console(file=io.StringIO(), force_terminal=False),
    )
    assert back_result == "handled"

    second_console_file = io.StringIO()
    second_console = Console(file=second_console_file, force_terminal=False)
    assert cli_mod._enter_forge_mode(
        root=focus_b,
        console=second_console,
        forge_state=forge_state,
    )
    assert forge_state.paths is not None
    assert forge_state.paths.root == repo.resolve()
    assert forge_state.paths.run_id == original_run_id
    assert forge_state.paths.focus_path == focus_b.resolve()
    assert forge_state.paths.focus_relpath == "packages/b"
    assert forge_state.paths.binding_requested_path == focus_b.resolve()
    workspace_context = json.loads(
        forge_state.paths.workspace_context_json_path.read_text(encoding="utf-8")
    )
    assert workspace_context["workspace_root"] == os.fspath(repo.resolve())
    assert workspace_context["focus_relpath"] == "packages/b"
    pointer = json.loads((repo / ".alysis" / "current_run.json").read_text(encoding="utf-8"))
    assert pointer["run_id"] == original_run_id
    assert pointer["focus_path"] == os.fspath(focus_b.resolve())
    assert pointer["focus_relpath"] == "packages/b"
    assert pointer["binding_requested_path"] == os.fspath(focus_b.resolve())
    assert (
        "Resumed this chat session's Forge run and rebound it to the current focus."
        in second_console_file.getvalue()
    )


def test_chat_forge_same_workspace_reentry_does_not_steal_pointer_from_different_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    focus_a = repo / "packages" / "a"
    focus_b = repo / "packages" / "b"
    focus_a.mkdir(parents=True)
    focus_b.mkdir(parents=True)
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)

    forge_state = cli_mod._ForgeChatState()
    first_console = Console(file=io.StringIO(), force_terminal=False)
    assert cli_mod._enter_forge_mode(
        root=focus_a,
        console=first_console,
        forge_state=forge_state,
    )
    assert forge_state.paths is not None
    session_run_id = forge_state.paths.run_id

    other_paths = create_plan_run(focus_a)
    write_current_run_pointer(other_paths)
    original_pointer = json.loads((repo / ".alysis" / "current_run.json").read_text("utf-8"))

    back_result = chat_impl_mod._handle_forge_chat_command_impl(
        cli_mod,
        input_text="/back",
        forge_state=forge_state,
        session=SimpleNamespace(),
        console=Console(file=io.StringIO(), force_terminal=False),
    )
    assert back_result == "handled"

    assert cli_mod._enter_forge_mode(
        root=focus_b,
        console=Console(file=io.StringIO(), force_terminal=False),
        forge_state=forge_state,
    )
    assert forge_state.paths is not None
    assert forge_state.paths.run_id == session_run_id
    assert forge_state.paths.focus_path == focus_b.resolve()

    pointer = json.loads((repo / ".alysis" / "current_run.json").read_text(encoding="utf-8"))
    assert pointer["run_id"] == other_paths.run_id
    assert pointer["focus_path"] == original_pointer["focus_path"]
    assert pointer["focus_relpath"] == original_pointer["focus_relpath"]
    assert pointer["binding_requested_path"] == original_pointer["binding_requested_path"]


def test_chat_forge_resume_still_uses_current_run_pointer_after_workspace_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    session_paths = create_plan_run(repo_a)
    pointer_paths = create_plan_run(repo_b)
    write_current_run_pointer(pointer_paths)
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)

    console_file = io.StringIO()
    forge_state = cli_mod._ForgeChatState(
        paths=session_paths,
        plan=load_plan(session_paths),
        entry_request_mode="resume_pointer",
    )
    console = Console(file=console_file, force_terminal=False)

    assert cli_mod._enter_forge_mode(root=repo_b, console=console, forge_state=forge_state)
    assert forge_state.paths is not None
    assert forge_state.paths.root == repo_b.resolve()
    assert forge_state.paths.run_id == pointer_paths.run_id
    assert "Resumed the current run pointer explicitly." in console_file.getvalue()


def test_forge_entry_status_explains_when_workspace_change_forces_fresh_run() -> None:
    assert (
        cli_mod._forge_entry_status_text(entry_kind="fresh_workspace_changed")
        == "Started a fresh Forge run because this chat moved to a different workspace."
    )


def test_forge_enter_command_accepts_goal_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console_file = io.StringIO()
    console = Console(file=console_file, force_terminal=False)
    session = SimpleNamespace(
        mode="review",
        stream=False,
        client=SimpleNamespace(model="test-model", temperature=1.0),
        cfg=AppConfig(model="test-model", default_mode="review"),
    )
    forge_state = cli_mod._ForgeChatState()
    captured: dict[str, Any] = {}
    chat_impl_mod._sync_cli_globals(cli_mod)

    def fake_enter_forge_mode(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(chat_impl_mod, "_enter_forge_mode", fake_enter_forge_mode)

    result = chat_impl_mod._handle_chat_command(
        input_text="/forge create a hello page",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=console,
        forge_state=forge_state,
    )

    assert result == "handled"
    rendered = console_file.getvalue()
    assert "Unknown /forge argument." not in rendered
    assert captured["forge_state"] is forge_state
    assert forge_state.entry_request_mode == "plain"
    assert forge_state.entry_goal == "create a hello page"


def test_chat_forge_entry_goal_shortcut_sets_project_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli_mod, "_prompt_forge_entry_plan_assistant", lambda *, console: False)
    console_file = io.StringIO()
    forge_state = cli_mod._ForgeChatState(entry_goal="create a hello page")

    assert cli_mod._enter_forge_mode(
        root=repo,
        console=Console(file=console_file, force_terminal=False),
        forge_state=forge_state,
    )

    assert forge_state.paths is not None
    plan = load_plan(forge_state.paths)
    assert plan["project_goal"] == "create a hello page"
    assert plan["summary"] == "create a hello page"
    assert plan.get("requirements") == []
    assert forge_state.entry_goal == ""
    assert "Project goal set from /forge." in console_file.getvalue()


def test_chat_forge_tui_entry_defaults_plan_assistant_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(
        mode="review",
        stream=False,
        _alysis_tui_interactive=True,
        client=SimpleNamespace(model="test-model", temperature=1.0),
        cfg=AppConfig(model="test-model", default_mode="review"),
    )
    captured: dict[str, Any] = {}
    chat_impl_mod._sync_cli_globals(cli_mod)

    def fake_enter_forge_mode(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(chat_impl_mod, "_enter_forge_mode", fake_enter_forge_mode)

    result = chat_impl_mod._handle_chat_command(
        input_text="/forge",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=io.StringIO(), force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert captured["planner_assistant_default"] is True


def test_parse_forge_enter_command_planner_flags() -> None:
    from alysis_code.cli_impl.commands.chat_state import (
        _parse_forge_enter_command,
    )

    # Bare + goal + resume keep their existing meaning (no explicit planner choice).
    assert _parse_forge_enter_command(cmd="/forge", arg="").planner_assistant_default is None
    assert (
        _parse_forge_enter_command(cmd="/forge", arg="build a page").planner_assistant_default
        is None
    )
    assert (
        _parse_forge_enter_command(cmd="/forge", arg="build a page").initial_goal == "build a page"
    )
    assert _parse_forge_enter_command(cmd="/forge", arg="resume").entry_mode == "resume_pointer"

    # Planner-choice flags set the default and are never treated as a goal.
    for on_flag in ("--planner", "planner", "PLANNER"):
        parsed = _parse_forge_enter_command(cmd="/forge", arg=on_flag)
        assert parsed.planner_assistant_default is True
        assert parsed.initial_goal == ""
    for off_flag in ("--no-planner", "no-planner", "No-Planner"):
        parsed = _parse_forge_enter_command(cmd="/forge", arg=off_flag)
        assert parsed.planner_assistant_default is False
        assert parsed.initial_goal == ""


def test_chat_forge_planner_flag_overrides_tui_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An explicit "/forge --no-planner" (emitted by the TUI intro popup's native
    # "Use the planner?" prompt) must force the assistant OFF even though the TUI
    # interactive default would otherwise turn it on.
    chat_impl_mod._sync_cli_globals(cli_mod)

    def _dispatch(arg_form: str) -> dict[str, Any]:
        session = SimpleNamespace(
            mode="review",
            stream=False,
            _alysis_tui_interactive=True,
            client=SimpleNamespace(model="test-model", temperature=1.0),
            cfg=AppConfig(model="test-model", default_mode="review"),
        )
        captured: dict[str, Any] = {}

        def fake_enter_forge_mode(**kwargs: Any) -> bool:
            captured.update(kwargs)
            return True

        monkeypatch.setattr(chat_impl_mod, "_enter_forge_mode", fake_enter_forge_mode)
        result = chat_impl_mod._handle_chat_command(
            input_text=arg_form,
            root=tmp_path,
            session=session,
            pending_images=[],
            console=Console(file=io.StringIO(), force_terminal=False),
            forge_state=cli_mod._ForgeChatState(),
        )
        assert result == "handled"
        return captured

    assert _dispatch("/forge --no-planner")["planner_assistant_default"] is False
    assert _dispatch("/forge --planner")["planner_assistant_default"] is True


def test_chat_forge_plan_assistant_default_suppresses_noninteractive_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        cli_mod,
        "_prompt_forge_entry_plan_assistant",
        lambda *, console: (_ for _ in ()).throw(AssertionError("prompt should be skipped")),
    )
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: True)
    console_file = io.StringIO()
    forge_state = cli_mod._ForgeChatState()

    assert cli_mod._enter_forge_mode(
        root=repo,
        console=Console(file=console_file, force_terminal=False),
        forge_state=forge_state,
        planner_assistant_default=True,
    )

    assert forge_state.assistant_enabled is True
    rendered = console_file.getvalue()
    assert "Plan Assistant defaulted off in non-interactive mode" not in rendered


def test_chat_forge_entry_uses_session_active_workdir_for_workspace_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    startup_focus = workspace_root / "repo-a"
    current_active = workspace_root / "repo-b"
    startup_focus.mkdir(parents=True)
    current_active.mkdir(parents=True)
    captured: dict[str, Path] = {}

    session = SimpleNamespace(
        root=workspace_root.resolve(),
        active_workdir_relpath="repo-b",
        mode="review",
        stream=False,
        client=SimpleNamespace(model="test-model", temperature=1.0),
        cfg=AppConfig(model="test-model", default_mode="review"),
    )

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = console, forge_state
        captured["root"] = root
        return True

    chat_impl_mod._sync_cli_globals(cli_mod)
    monkeypatch.setattr(chat_impl_mod, "_enter_forge_mode", fake_enter_forge_mode)

    result = chat_impl_mod._handle_chat_command(
        input_text="/forge",
        root=startup_focus,
        session=session,
        pending_images=[],
        console=Console(file=io.StringIO(), force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert captured["root"] == current_active.resolve()


def test_chat_forge_entry_rejects_invalid_session_active_workdir_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    startup_focus = workspace_root / "repo-a"
    startup_focus.mkdir(parents=True)
    console_file = io.StringIO()
    session = SimpleNamespace(
        root=workspace_root.resolve(),
        active_workdir_relpath="../outside",
        mode="review",
        stream=False,
        client=SimpleNamespace(model="test-model", temperature=1.0),
        cfg=AppConfig(model="test-model", default_mode="review"),
    )

    def fail_enter_forge_mode(**_kwargs: Any) -> bool:
        raise AssertionError("should not enter forge when active workdir resolution fails")

    chat_impl_mod._sync_cli_globals(cli_mod)
    monkeypatch.setattr(chat_impl_mod, "_enter_forge_mode", fail_enter_forge_mode)

    result = chat_impl_mod._handle_chat_command(
        input_text="/forge",
        root=startup_focus,
        session=session,
        pending_images=[],
        console=Console(file=console_file, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    rendered = console_file.getvalue()
    assert "Failed to enter Forge:" in rendered
    assert " ".join(rendered.split()) == (
        "Failed to enter Forge: Active workdir must stay inside the bound workspace root."
    )


def test_plan_command_in_forge_mode_uses_forge_plan_handler(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    _install_dummy_forge_entry(monkeypatch, tmp_path)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/plan add tests\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    _assert_forge_plan_command_guidance(result.output)
    assert "Back to chat." in result.output


def test_plan_command_in_forge_mode_guidance_is_wrap_stable_under_narrow_capture(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    _install_dummy_forge_entry(monkeypatch, tmp_path)

    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/plan add tests\n/back\nexit\n",
        env=_env(tmp_path),
        terminal_width=36,
    )

    assert result.exit_code == 0
    _assert_forge_plan_command_guidance(result.output)


def test_help_command_in_forge_mode_shows_forge_commands(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
        notes_dir = plan_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        forge_state.ui_mode = "forge"
        forge_state.paths = SimpleNamespace(
            run_id="wf",
            plan_json_path=plan_dir / "plan.json",
            plan_md_path=plan_dir / "PLAN.md",
            notes_dir=notes_dir,
            notes_path=notes_dir / "user_notes.md",
        )
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "x",
            "summary": "x",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/help\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Forge Commands" in result.output
    assert "/execute plan" in result.output
    assert "/goal <text>" in result.output
    assert "/report" in result.output
    _assert_forge_help_mentions_plan_views(result.output)
    assert "plan-first" not in result.output


def test_chat_help_panel_in_forge_mode_includes_forge_commands() -> None:
    console = Console(record=True, width=140)

    console.print(cli_mod._chat_help_panel(ui_mode="forge"))
    rendered = console.export_text()

    assert "Forge Commands" in rendered
    assert "/execute plan" in rendered
    assert "/goal <text>" in rendered
    assert "/report" in rendered
    assert "/show" in rendered
    _assert_forge_help_mentions_plan_views(rendered)
    assert "/assistant on|off|status" not in rendered
    assert "<free text>" not in rendered
    assert "Type freely to add requirements or talk to the planner." in rendered
    assert "/forge [resume]" not in rendered
    assert "plan-first" not in rendered


def test_forge_task_status_markup_uses_monochrome_status_emphasis() -> None:
    assert cli_mod._forge_task_status_markup("done") == "[bold]done[/bold]"
    assert cli_mod._forge_task_status_markup("failed") == "[red]failed[/red]"
    assert (
        cli_mod._forge_task_status_markup("candidate_rejected") == "[red]candidate_rejected[/red]"
    )
    assert (
        cli_mod._forge_task_status_markup("blocked_integration") == "[red]blocked_integration[/red]"
    )
    assert cli_mod._forge_task_status_markup("in_progress") == "in_progress"
    assert cli_mod._forge_task_status_markup("planned") == "[dim]planned[/dim]"


def test_forge_task_status_counts_classifies_satisfied_and_interrupted_tasks() -> None:
    plan = {
        "tasks": [
            {"id": "T01", "status": "done"},
            {"id": "T02", "status": "already_satisfied"},
            {"id": "T03", "status": "interrupted"},
            {"id": "T04", "status": "blocked_integration"},
            {"id": "T05", "status": "planned"},
        ]
    }

    assert cli_mod._forge_task_status_counts(plan) == (2, 2, 1)


def test_forge_usage_merges_planner_and_execution_logs(tmp_path: Path) -> None:
    appended: list[tuple[str, dict[str, Any]]] = []

    class _Store:
        def append(self, event_type: str, payload: dict[str, Any]) -> None:
            appended.append((event_type, payload))

    session = SimpleNamespace(usage_summary=UsageSummary(), store=_Store())
    planner_payload = {
        "event_type": "llm_usage",
        "timestamp": "2026-01-01T00:00:00Z",
        "role": "forge_planner",
        "requested_model": "planner-model",
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "usage_source": "api",
    }
    assert (
        chat_commands_mod._merge_usage_payloads_into_session(
            session=session,
            payloads=[planner_payload],
        )
        == 1
    )

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    worker_payload = {
        "event_type": "llm_usage",
        "timestamp": "2026-01-01T00:00:01Z",
        "role": "swarm_worker:T01",
        "requested_model": "worker-model",
        "prompt_tokens": 20,
        "completion_tokens": 6,
        "total_tokens": 26,
        "usage_source": "api",
    }
    (logs_dir / "T01.jsonl").write_text(
        json.dumps({"type": "llm_usage", "payload": worker_payload}) + "\n",
        encoding="utf-8",
    )

    paths = SimpleNamespace(execution_logs_dir=logs_dir)
    assert (
        chat_commands_mod._merge_forge_execution_usage_into_session(session=session, paths=paths)
        == 1
    )
    assert (
        chat_commands_mod._merge_forge_execution_usage_into_session(session=session, paths=paths)
        == 0
    )

    totals = session.usage_summary.totals()
    assert totals["calls"] == 2
    assert totals["prompt_tokens"] == 30
    assert totals["completion_tokens"] == 10
    assert totals["total_tokens"] == 40
    assert [event_type for event_type, _payload in appended] == ["llm_usage", "llm_usage"]
    assert {payload["role"] for _event_type, payload in appended} == {
        "forge_planner",
        "swarm_worker:T01",
    }


def test_forge_plan_markdown_command_is_handled(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"

    class _DummySession:
        store = _DummyStore()
        stream = False
        mode = "review"

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "x",
            "summary": "x",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        forge_state.paths.plan_md_path.write_text(
            "# PLAN\n\nHello plan markdown.\n",
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/plan markdown\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "PLAN.md:" in result.output


def test_forge_plan_markdown_falls_back_to_preview_when_pager_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    paths = SimpleNamespace(
        plan_json_path=plan_dir / "plan.json",
        plan_md_path=plan_dir / "PLAN.md",
    )
    plan = {
        "run_id": "wf",
        "project_goal": "Goal",
        "summary": "Summary",
        "requirements": ["Req 1"],
        "tasks": [],
        "assets": [],
    }

    console = Console(width=120, record=True, color_system=None, force_terminal=False)
    monkeypatch.setattr(cli_mod, "_is_interactive_terminal", lambda: True)

    def _broken_pager(_self: Console, *args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs

        class _Ctx:
            def __enter__(self) -> None:
                raise RuntimeError("pager boom")

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                _ = exc_type, exc, tb
                return False

        return _Ctx()

    monkeypatch.setattr(Console, "pager", _broken_pager)

    cli_mod._show_forge_plan_markdown(console=console, paths=paths, plan=plan)
    rendered = console.export_text()
    assert "PLAN.md:" in rendered
    assert "Pager unavailable:" in rendered
    assert "Showing preview instead." in rendered


def test_forge_plan_markdown_shows_quit_hint_inside_pager(tmp_path: Path, monkeypatch) -> None:
    plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    paths = SimpleNamespace(
        plan_json_path=plan_dir / "plan.json",
        plan_md_path=plan_dir / "PLAN.md",
    )
    plan = {
        "run_id": "wf",
        "project_goal": "Goal",
        "summary": "Summary",
        "requirements": ["Req 1"],
        "tasks": [],
        "assets": [],
    }

    console = Console(width=120, record=True, color_system=None, force_terminal=False)
    monkeypatch.setattr(cli_mod, "_is_interactive_terminal", lambda: True)

    def _noop_pager(_self: Console, *args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs

        class _Ctx:
            def __enter__(self) -> None:
                return None

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                _ = exc_type, exc, tb
                return False

        return _Ctx()

    monkeypatch.setattr(Console, "pager", _noop_pager)

    cli_mod._show_forge_plan_markdown(console=console, paths=paths, plan=plan)
    rendered = console.export_text()
    assert "PLAN.md:" in rendered
    assert "Press q to exit this view." in rendered


def test_forge_execute_plan_runs_swarm_with_session_wiring(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"swarm_calls": []}
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model", integration_verify_mode="strict")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = load_plan(paths)
        return True

    def fake_run_swarm(**kwargs: Any) -> int:
        captured["swarm_calls"].append(kwargs)
        assert kwargs.get("mode") == "auto"
        assert kwargs.get("api_key_override") == "k"
        assert kwargs.get("scope_mode") == "strict"
        plan = kwargs.get("plan") or {}
        assert len(plan.get("tasks") or []) >= 1
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\nBuild src/dashboard.py with login and reporting\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert len(captured["swarm_calls"]) == 1
    call = captured["swarm_calls"][0]
    assert call.get("mode") == "auto"
    assert call.get("api_key_override") == "k"
    assert call.get("parallel") == 2
    assert call.get("max_steps") is None
    assert call.get("trace_level") == "compact"
    assert call.get("trace_sink") is not None
    assert call.get("dry_run") is False
    assert call.get("cfg").integration_verify_mode == "strict"
    assert call.get("integration_mode") is None


def test_execute_plan_refuses_execution_unready_mutating_task_after_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["tasks"].append(
        {
            "id": "T01",
            "title": "Fix login bug",
            "description": "Update auth flow.",
            "acceptance_criteria": ["Login works."],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        }
    )
    save_plan(paths, plan)

    monkeypatch.setattr(
        cli_mod,
        "run_swarm",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("run_swarm should not be called for execution-unready tasks")
        ),
    )
    chat_impl_mod._sync_cli_globals(cli_mod)

    class _CaptureConsole:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def print(self, renderable: Any = "", *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            self.messages.append(str(renderable))

    console = _CaptureConsole()
    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )

    result = chat_impl_mod._handle_forge_chat_command(
        input_text="/execute plan",
        forge_state=forge_state,
        session=SimpleNamespace(),
        console=console,
    )

    assert result == "handled"
    rendered = "\n".join(console.messages)
    assert "Execution blocked" in rendered
    assert "runnable or ambiguous task lacks runnable estimated_files/write_scope" in rendered


@pytest.mark.parametrize(
    ("task", "expected_rule"),
    [
        (
            {
                "id": "T01",
                "title": "Fix calculator seed behavior",
                "description": "Update implementation code.",
                "acceptance_criteria": ["Calculator seed behavior is implemented."],
                "dependencies": [],
                "estimated_files": [".alysis/something.json"],
                "write_scope": [".alysis/something.json"],
                "branch": "",
                "status": "planned",
                "attempts": 0,
            },
            "R4",
        ),
        (
            {
                "id": "T01",
                "title": "Fix calculator behavior",
                "description": "Update implementation code.",
                "acceptance_criteria": ["Calculator behavior is fixed."],
                "dependencies": [],
                "estimated_files": ["README.md"],
                "write_scope": ["README.md"],
                "branch": "",
                "status": "planned",
                "attempts": 0,
            },
            "R3",
        ),
        (
            {
                "title": "Fix calculator division",
                "description": "Update implementation code.",
                "acceptance_criteria": ["Division by zero raises ValueError."],
                "dependencies": [],
                "estimated_files": ["src/calc.py"],
                "write_scope": ["src/calc.py"],
                "branch": "",
                "status": "planned",
                "attempts": 0,
            },
            "R4",
        ),
    ],
)
def test_execute_plan_blocks_validation_failures_before_swarm(
    tmp_path: Path,
    monkeypatch,
    task: dict[str, Any],
    expected_rule: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["tasks"].append(task)
    save_plan(paths, plan)

    monkeypatch.setattr(
        cli_mod,
        "run_swarm",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("run_swarm should not be called for rejected plans")
        ),
    )
    chat_impl_mod._sync_cli_globals(cli_mod)

    class _CaptureConsole:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def print(self, renderable: Any = "", *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            self.messages.append(str(renderable))

    console = _CaptureConsole()
    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )

    result = chat_impl_mod._handle_forge_chat_command(
        input_text="/execute plan",
        forge_state=forge_state,
        session=SimpleNamespace(),
        console=console,
    )

    assert result == "handled"
    rendered = "\n".join(console.messages)
    assert "Execution blocked" in rendered
    assert expected_rule in rendered


def test_execute_plan_allows_report_only_task_before_swarm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["tasks"].append(
        {
            "id": "T01",
            "title": "Investigate login issue",
            "description": "Read-only analysis only; report findings.",
            "acceptance_criteria": ["Findings documented."],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        }
    )
    save_plan(paths, plan)

    calls: list[dict[str, Any]] = []

    def fake_run_swarm(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    chat_impl_mod._sync_cli_globals(cli_mod)

    class _CaptureConsole:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def print(self, renderable: Any = "", *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            self.messages.append(str(renderable))

    console = _CaptureConsole()
    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )
    session = SimpleNamespace(
        cfg=AppConfig(model="test-model"),
        client=SimpleNamespace(api_key=""),
        store=SimpleNamespace(enabled=False),
        yes=False,
    )

    result = chat_impl_mod._handle_forge_chat_command(
        input_text="/execute plan",
        forge_state=forge_state,
        session=session,
        console=console,
    )

    rendered = "\n".join(console.messages)
    assert result == "handled"
    assert "Execution blocked" not in rendered
    assert len(calls) == 1
    assert calls[0]["plan"]["tasks"][0]["title"] == "Investigate login issue"


def test_forge_execute_plan_reports_timeout_when_same_run_stays_locked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    plan_snapshot = paths.plan_json_path.read_text(encoding="utf-8")
    plan_md_snapshot = paths.plan_md_path.read_text(encoding="utf-8")
    notes_snapshot = paths.notes_path.read_text(encoding="utf-8")

    write_run_mutation_lock_metadata(
        paths.run_dir / "active_execution.lock.json",
        {
            "schema_version": 1,
            "run_id": paths.run_id,
            "mode": "forge_swarm:other",
            "kind": "lock",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": "2026-03-26T00:00:00+00:00",
            "owner_token": "other-owner",
            "workspace_root": os.fspath(paths.root),
            "run_dir": os.fspath(paths.run_dir),
        },
    )

    monkeypatch.setattr(forge_mod, "now_iso", lambda: "2035-01-01T00:00:00+00:00")
    monkeypatch.setenv("ALYSIS_FORGE_LOCK_WAIT_TIMEOUT_S", "0")
    monkeypatch.setattr(
        cli_mod,
        "run_swarm",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("run_swarm should not be called when the run is already locked")
        ),
    )
    chat_impl_mod._sync_cli_globals(cli_mod)

    class _CaptureConsole:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def print(self, renderable: Any = "", *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            self.messages.append(str(renderable))

    console = _CaptureConsole()
    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )
    session = SimpleNamespace()

    execute_result = chat_impl_mod._handle_forge_chat_command(
        input_text="/execute plan",
        forge_state=forge_state,
        session=session,
        console=console,
    )
    back_result = chat_impl_mod._handle_forge_chat_command(
        input_text="/back",
        forge_state=forge_state,
        session=session,
        console=console,
    )

    assert execute_result == "handled"
    assert back_result == "handled"
    assert "Wait for the active execution to finish" in "\n".join(console.messages)
    assert paths.plan_json_path.read_text(encoding="utf-8") == plan_snapshot
    assert paths.plan_md_path.read_text(encoding="utf-8") == plan_md_snapshot
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert notes_text.startswith(notes_snapshot)
    assert "Forge /execute plan failed" not in notes_text
    assert "/execute plan" not in notes_text
    assert list(paths.execution_dir.glob("worker_results/*.json")) == []
    assert list(paths.execution_dir.glob("merge_results/*.json")) == []
    assert not (paths.execution_dir / "swarm_summary.md").exists()


def test_forge_back_does_not_rewrite_plan_when_session_makes_no_semantic_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan_snapshot = paths.plan_json_path.read_text(encoding="utf-8")
    plan_md_snapshot = paths.plan_md_path.read_text(encoding="utf-8")

    monkeypatch.setattr(forge_mod, "now_iso", lambda: "2035-01-01T00:00:00+00:00")
    chat_impl_mod._sync_cli_globals(cli_mod)
    console = Console(file=io.StringIO(), force_terminal=False)
    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )
    session = SimpleNamespace()

    result = chat_impl_mod._handle_forge_chat_command(
        input_text="/back",
        forge_state=forge_state,
        session=session,
        console=console,
    )

    assert result == "handled"
    assert paths.plan_json_path.read_text(encoding="utf-8") == plan_snapshot
    assert paths.plan_md_path.read_text(encoding="utf-8") == plan_md_snapshot


def test_forge_back_preserves_real_semantic_edits_made_before_exit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    chat_impl_mod._sync_cli_globals(cli_mod)
    console = Console(file=io.StringIO(), force_terminal=False)
    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )
    session = SimpleNamespace()

    goal_result = chat_impl_mod._handle_forge_chat_command(
        input_text="/goal Ship the admin dashboard",
        forge_state=forge_state,
        session=session,
        console=console,
    )
    back_result = chat_impl_mod._handle_forge_chat_command(
        input_text="/back",
        forge_state=forge_state,
        session=session,
        console=console,
    )

    assert goal_result == "handled"
    assert back_result == "handled"
    plan = load_plan(paths)
    assert plan["project_goal"] == "Ship the admin dashboard"
    assert "Ship the admin dashboard" in paths.plan_md_path.read_text(encoding="utf-8")


def test_forge_execute_plan_reports_human_summary(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship dashboard",
            "summary": "Deliver the initial dashboard release.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Build UI",
                    "status": "planned",
                    "estimated_files": ["src/ui.py"],
                    "write_scope": ["src/ui.py"],
                },
                {
                    "id": "T02",
                    "title": "Add auth",
                    "status": "planned",
                    "estimated_files": ["src/auth.py"],
                    "write_scope": ["src/auth.py"],
                },
                {
                    "id": "T03",
                    "title": "Write tests",
                    "status": "planned",
                    "estimated_files": ["src/auth.py", "tests/test_auth.py"],
                    "write_scope": ["src/auth.py", "tests/test_auth.py"],
                },
            ],
            "assets": [],
        }
        return True

    def fake_run_swarm(**kwargs: Any) -> int:
        plan = kwargs["plan"]
        plan["tasks"][0]["status"] = "done"
        plan["tasks"][1]["status"] = "failed"
        plan["tasks"][2]["status"] = "in_progress"
        cli_mod.save_plan(kwargs["paths"], plan)
        return 1

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Execution finished with issues" in result.output
    assert "Tasks · 3 total · 1 done · 1 failed · 1 remaining" in result.output
    assert "Summary ·" in result.output
    assert "Swarm exit code:" not in result.output
    assert "Swarm summary:" not in result.output


def test_finalize_forge_plan_uses_bar_warning_format(tmp_path: Path, monkeypatch) -> None:
    paths = _forge_run_paths(tmp_path)
    plan = {
        "run_id": "wf",
        "project_goal": "Goal",
        "summary": "Summary",
        "requirements": [],
        "tasks": [{"id": "T01", "title": "Task", "status": "planned"}],
        "assets": [],
    }
    console = Console(record=True, width=120)

    monkeypatch.setattr(
        cli_mod,
        "_reconcile_plan_for_paths",
        lambda **_kwargs: (
            SimpleNamespace(changed=False, updated_task_ids=[], warnings=["missing dependency"]),
            None,
        ),
    )
    monkeypatch.setattr(cli_mod, "validate_plan", lambda _plan: ["missing acceptance criteria"])

    cli_mod._finalize_forge_plan(console=console, paths=paths, plan=plan)
    rendered = console.export_text()

    assert "Plan reconciliation: missing dependency" in rendered
    assert "Plan validation: missing acceptance criteria" in rendered
    assert "Plan reconciliation warnings:" not in rendered
    assert "Plan validation warnings:" not in rendered


def test_forge_execute_plan_prints_validation_warnings_once_after_enrichment(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship dashboard",
            "summary": "Deliver the initial dashboard release.",
            "requirements": [],
            "tasks": [{"id": "T01", "title": "Build UI", "status": "planned"}],
            "assets": [],
        }
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="No structured changes needed.",
            questions=[],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "_forge_enrich_plan_enabled", lambda: True)
    monkeypatch.setattr(cli_mod, "validate_plan", lambda _plan: ["missing acceptance_criteria"])
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(cli_mod, "run_swarm", lambda **_kwargs: 0)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert result.output.count("Plan validation: missing acceptance_criteria") == 1


def test_forge_execute_plan_swarm_trace_compact(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"progress": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummySurface:
        trace_level = "compact"

        @staticmethod
        def on_progress_update(message: str) -> None:
            captured["progress"].append(message)

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        surface = _DummySurface()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_swarm(**kwargs: Any) -> int:
        sink = kwargs["trace_sink"]
        sink.emit(
            build_swarm_trace_event(
                run_id="wf",
                task_id="T01",
                phase="worker.lifecycle",
                message="Worker started.",
            )
        )
        sink.emit(
            build_swarm_trace_event(
                run_id="wf",
                task_id="T01",
                phase="worker.output",
                message="Full-only detail should stay hidden.",
                verbosity="full",
            )
        )
        sink.close()
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\nBuild src/landing.py landing page\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert any("[T01] Worker started." in msg for msg in captured["progress"])
    assert not any("Full-only detail should stay hidden." in msg for msg in captured["progress"])


def test_forge_execute_plan_swarm_trace_full(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"progress": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummySurface:
        trace_level = "compact"

        @staticmethod
        def on_progress_update(message: str) -> None:
            captured["progress"].append(message)

        @classmethod
        def set_trace_level(cls, level: str) -> str:
            cls.trace_level = level
            return level

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        surface = _DummySurface()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_swarm(**kwargs: Any) -> int:
        sink = kwargs["trace_sink"]
        sink.emit(
            build_swarm_trace_event(
                run_id="wf",
                task_id="T02",
                phase="worker.lifecycle",
                message="Worker started.",
            )
        )
        sink.emit(
            build_swarm_trace_event(
                run_id="wf",
                task_id="T02",
                phase="worker.output",
                message="Detailed worker progress is visible.",
                verbosity="full",
            )
        )
        sink.close()
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/trace full\nBuild src/landing.py landing page\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert any("[T02] Worker started." in msg for msg in captured["progress"])
    assert any("Detailed worker progress is visible." in msg for msg in captured["progress"])


def test_forge_execute_plan_swarm_trace_off(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"progress": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummySurface:
        trace_level = "compact"

        @staticmethod
        def on_progress_update(message: str) -> None:
            captured["progress"].append(message)

        @classmethod
        def set_trace_level(cls, level: str) -> str:
            cls.trace_level = level
            return level

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        surface = _DummySurface()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_swarm(**kwargs: Any) -> int:
        sink = kwargs["trace_sink"]
        sink.emit(
            build_swarm_trace_event(
                run_id="wf",
                task_id="T03",
                phase="worker.lifecycle",
                message="Worker started.",
            )
        )
        sink.close()
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/trace off\nBuild a landing page\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["progress"] == []


def test_forge_planner_uses_session_api_key_and_cfg(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "x",
            "summary": "x",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        return SimpleNamespace(
            assistant_message="Planner response",
            questions=["Which auth provider should we use?"],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease design the auth flow\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 1
    planner_call = captured["planner_calls"][0]
    assert planner_call.get("api_key_override") == "k"
    cfg = planner_call.get("cfg")
    assert isinstance(cfg, AppConfig)
    assert cfg.model == "session-model"
    assert cfg is not _DummySession.cfg
    workspace_context = planner_call.get("workspace_context")
    assert isinstance(workspace_context, dict)
    assert workspace_context.get("focus_relpath") == "."


def test_forge_planner_recovers_after_transient_request_retry_without_duplicate_apply(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {
        "plan_ref": None,
        "paths": None,
        "planner_calls": 0,
    }

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="planner-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "x",
            "summary": "x",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = forge_state.paths
        return True

    class FakePlannerClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["planner_calls"] += 1
            if captured["planner_calls"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            payload = {
                "assistant_message": "Recovered planner response.",
                "questions": [],
                "plan_update": {
                    "tasks_add": [
                        {
                            "title": "Add recovered planner task",
                            "description": "Apply the recovered planner update once.",
                            "acceptance_criteria": ["Task exists once"],
                            "dependencies": [],
                            "estimated_files": ["src/example.py"],
                            "write_scope": ["src/example.py"],
                        }
                    ]
                },
            }
            return SimpleNamespace(content=json.dumps(payload))

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(
        "alysis_code.plan_assistant.OpenAICompatClient",
        FakePlannerClient,
    )
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease update the plan\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Recovered planner response." in result.output
    assert "Applied planner update to plan." in result.output
    assert "Planner request recovered after 1 transient retry." in result.output
    assert captured["planner_calls"] == 3
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 1
    assert plan_ref["tasks"][0]["title"] == "Add recovered planner task"
    paths = captured["paths"]
    assert paths is not None
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert (
        notes_text.count("Planner warning: Planner request recovered after 1 transient retry.") == 1
    )


def test_forge_small_talk_routes_through_planner_when_assistant_on(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": 0, "plan_ref": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        captured["planner_calls"] += 1
        return SimpleNamespace(
            assistant_message="Hi. Tell me what you want to plan.",
            questions=[],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nhello\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["planner_calls"] == 1
    assert "Hi. Tell me what you want to plan." in result.output
    assert "Captured requirement note (planner produced no structured update)." not in result.output
    plan_ref = captured["plan_ref"] or {}
    requirements = plan_ref.get("requirements") or []
    assert requirements == []


def test_forge_greek_small_talk_routes_through_planner_when_assistant_on(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": 0, "plan_ref": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        captured["planner_calls"] += 1
        return SimpleNamespace(
            assistant_message="Γεια. Πες μου τι θέλεις να σχεδιάσουμε.",
            questions=[],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nγεια\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["planner_calls"] == 1
    assert "Γεια. Πες μου τι θέλεις να σχεδιάσουμε." in result.output
    assert "Captured requirement note (planner produced no structured update)." not in result.output
    plan_ref = captured["plan_ref"] or {}
    requirements = plan_ref.get("requirements") or []
    assert requirements == []


def test_forge_assistant_off_sets_empty_goal_from_free_text(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": 0, "plan_ref": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        captured["planner_calls"] += 1
        raise AssertionError("run_planner_turn should not be called when assistant is off")

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\na one-page hello world website\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["planner_calls"] == 0
    assert "Project goal set." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert plan_ref.get("project_goal") == "a one-page hello world website"
    assert plan_ref.get("summary") == "a one-page hello world website"
    requirements = plan_ref.get("requirements") or []
    assert requirements == ["a one-page hello world website"]


def test_forge_no_plan_update_without_error_does_not_capture_requirement(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": 0, "plan_ref": None, "fallback_calls": 0}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        captured["planner_calls"] += 1
        return SimpleNamespace(
            assistant_message="Need one more detail before I update the plan.",
            questions=["What is the target runtime?"],
            plan_update=None,
            error=None,
        )

    def fake_capture_fallback(**_kwargs: Any) -> bool:
        captured["fallback_calls"] += 1
        return False

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(
        cli_mod, "_capture_forge_requirement_from_planner_fallback", fake_capture_fallback
    )
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\ntell me a joke\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["planner_calls"] == 1
    assert captured["fallback_calls"] == 0
    assert "You:" not in result.output
    assert "tell me a joke" in result.output
    assert "Planner:" in result.output
    assert "Need one more detail before I update the plan." in result.output
    assert "- What is the target runtime?" in result.output
    assert "Captured requirement note (planner produced no structured update)." not in result.output
    plan_ref = captured["plan_ref"] or {}
    requirements = plan_ref.get("requirements") or []
    assert requirements == []


def test_forge_noop_plan_update_does_not_capture_requirement(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": 0, "plan_ref": None, "fallback_calls": 0}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": ["keep legacy flags"],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        captured["planner_calls"] += 1
        return SimpleNamespace(
            assistant_message="I will keep the current plan.",
            questions=[],
            plan_update={"requirements_append": ["keep legacy flags"]},
            error=None,
        )

    def fake_capture_fallback(**_kwargs: Any) -> bool:
        captured["fallback_calls"] += 1
        return False

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(
        cli_mod, "_capture_forge_requirement_from_planner_fallback", fake_capture_fallback
    )
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nImplement robust parser planning\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["planner_calls"] == 1
    assert captured["fallback_calls"] == 0
    assert "Planner:" in result.output
    assert "I will keep the current plan." in result.output
    assert "Planner update contained no applicable changes." in result.output
    assert "Captured requirement note (planner produced no structured update)." not in result.output
    plan_ref = captured["plan_ref"] or {}
    requirements = plan_ref.get("requirements") or []
    assert requirements == ["keep legacy flags"]


def test_forge_planner_follow_up_keeps_protected_history_immutable(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship login safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship login",
                    "description": "Released in src/login.py.",
                    "acceptance_criteria": [],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="I will add a login follow-up task.",
            questions=[],
            plan_update={
                "tasks_update": [{"id": "T01", "title": "Rewrite shipped login"}],
                "tasks_add": [
                    {
                        "title": "Add login follow-up",
                        "description": "Track post-release login work separately.",
                        "acceptance_criteria": ["Follow-up task exists"],
                        "dependencies": ["T01"],
                        "estimated_files": ["src/login.py", "tests/test_login.py"],
                        "write_scope": ["src/login.py", "tests/test_login.py"],
                    }
                ],
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease add a login follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    assert "Planner update ignored because this message did not look planning-related" not in (
        result.output
    )
    assert "protected non-planned task history" in result.output
    plan_ref = captured["plan_ref"] or {}
    assert plan_ref["tasks"][0]["title"] == "Ship login"
    assert plan_ref["tasks"][0]["status"] == "done"
    assert plan_ref["tasks"][0]["estimated_files"] == []
    assert plan_ref["tasks"][0]["write_scope"] == []
    assert plan_ref["tasks"][1]["title"] == "Add login follow-up"
    paths = captured["paths"]
    assert paths is not None
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "Planner warning:" in notes_text
    assert "follow-up work as new tasks instead" in notes_text


def test_forge_planner_follow_up_synthesizes_task_from_protected_update_only(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship src/slugify.js",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="I will track the slugify follow-up separately.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": "Add slugify follow-up tests",
                        "description": "Track post-release slugify tests separately.",
                        "acceptance_criteria": ["Slugify follow-up tests exist"],
                        "dependencies": ["T01"],
                        "estimated_files": ["test/slugify.test.js"],
                        "write_scope": ["test/slugify.test.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease add a slugify follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    assert "protected non-planned task history" in result.output
    assert "Synthesized new planned follow-up tasks from rejected protected" in result.output
    assert "updates" in result.output
    assert "T02" in result.output
    plan_ref = captured["plan_ref"] or {}
    assert plan_ref["tasks"][0]["title"] == "Ship src/slugify.js"
    assert plan_ref["tasks"][0]["status"] == "done"
    assert plan_ref["tasks"][1]["id"] == "T02"
    assert plan_ref["tasks"][1]["status"] == "planned"
    assert plan_ref["tasks"][1]["title"] == "Add slugify follow-up tests"
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in planner_summary
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "Planner update:" in notes_text
    assert "synthesized follow-up tasks: T02" in notes_text


def test_forge_planner_follow_up_synthesizes_same_file_task_from_protected_update(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="I will track the lowercase option as same-file follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": "Add lowercase option follow-up",
                        "description": "Update src/slugify.js to add lowercase: bool = True behavior.",
                        "acceptance_criteria": ["src/slugify.js supports lowercase option"],
                        "dependencies": ["T01"],
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--allow-broad-workspace",
        ],
        input="/forge\n/assistant on\nPlease add a slugify lowercase follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    assert "protected non-planned task history" in result.output
    assert "Synthesized new planned follow-up tasks from rejected protected" in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 2
    assert plan_ref["tasks"][0]["title"] == "Ship slugify"
    assert plan_ref["tasks"][1]["title"] == "Add lowercase option follow-up"
    assert plan_ref["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert plan_ref["tasks"][1]["write_scope"] == ["src/slugify.js"]
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in planner_summary
    assert "missing new runnable delta beyond protected history" not in planner_summary
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in notes_text
    assert "missing new runnable delta beyond protected history" not in notes_text


def test_forge_planner_follow_up_synthesizes_title_only_same_file_task(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="I will track the lowercase option as same-file follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": "Add lowercase option follow-up",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--allow-broad-workspace",
        ],
        input="/forge\n/assistant on\nPlease add a slugify lowercase follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 2
    assert plan_ref["tasks"][1]["title"] == "Add lowercase option follow-up"
    assert plan_ref["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert plan_ref["tasks"][1]["write_scope"] == ["src/slugify.js"]
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in planner_summary
    assert "missing new runnable delta beyond protected history" not in planner_summary


def test_forge_planner_follow_up_synthesizes_title_only_new_path_task(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="I will track the docs follow-up as new work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": "Add docs follow-up",
                        "estimated_files": ["docs/slugify.md"],
                        "write_scope": ["docs/slugify.md"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--allow-broad-workspace",
        ],
        input="/forge\n/assistant on\nPlease add a slugify docs follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 2
    assert plan_ref["tasks"][1]["title"] == "Add docs follow-up"
    assert plan_ref["tasks"][1]["estimated_files"] == ["docs/slugify.md"]
    assert plan_ref["tasks"][1]["write_scope"] == ["docs/slugify.md"]
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in planner_summary
    assert "missing new runnable delta beyond protected history" not in planner_summary


def test_forge_planner_follow_up_refuses_weak_generic_title_with_scope(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="That protected update does not describe runnable new follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": "Refactor src/slugify.js",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        [
            "chat",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--allow-broad-workspace",
        ],
        input="/forge\n/assistant on\nPlease add a slugify follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 1
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )


def test_forge_planner_follow_up_synthesizes_same_file_task_from_description_delta(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="I will track the lowercase option as same-file follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "description": "Add lowercase option behavior with default True.",
                        "dependencies": ["T01"],
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease add a slugify lowercase follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 2
    assert plan_ref["tasks"][1]["title"] == "Ship slugify follow-up"
    assert plan_ref["tasks"][1]["description"] == "Add lowercase option behavior with default True."
    assert plan_ref["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert plan_ref["tasks"][1]["write_scope"] == ["src/slugify.js"]
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in planner_summary
    assert "missing new runnable delta beyond protected history" not in planner_summary


def test_forge_planner_follow_up_refuses_trivial_protected_update_without_new_path_signal(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship src/slugify.js",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="That protected task update does not describe runnable new work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": "Ship src/slugify.js",
                        "description": "Minor wording tweak only.",
                        "estimated_files": ["src/slugify.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease add a slugify follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 1
    assert plan_ref["tasks"][0]["title"] == "Ship src/slugify.js"
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "protected non-planned task history" in notes_text
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in notes_text
    )


def test_forge_planner_follow_up_refuses_punctuation_only_same_file_delta(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="That protected update does not describe runnable new follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "description": "Released in src/slugify.js!",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease add a slugify follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 1
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "protected non-planned task history" in notes_text
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in notes_text
    )


def test_forge_planner_follow_up_refuses_formatting_only_same_file_delta(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                }
            ],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="That protected update does not describe runnable new follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "description": "Formatting only in src/slugify.js.",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease add a slugify follow-up task\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref["tasks"]) == 1
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )


def test_forge_router_offtopic_result_leaves_plan_unchanged(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"plan_ref": None, "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        captured["paths"] = forge_state.paths
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="I am your planner assistant. Share the feature scope you want to build.",
            questions=[],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nΠώς σε λένε;\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert (
        "I am your planner assistant. Share the feature scope you want to build." in result.output
    )
    assert "Applied planner update to plan." not in result.output
    plan_ref = captured["plan_ref"] or {}
    assert len(plan_ref.get("tasks") or []) == 0
    paths = captured["paths"]
    assert paths is not None
    assert "no plan_update proposed" in paths.planner_summary_path.read_text(encoding="utf-8")


def test_forge_clarification_follow_up_accepts_terse_planning_answer(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "paths": None, "plan_ref": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["paths"] = paths
        captured["plan_ref"] = forge_state.plan
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        if len(captured["planner_calls"]) == 1:
            return SimpleNamespace(
                assistant_message="Need one quick clarification before I update the plan.",
                questions=["Which stack and theme should we use?"],
                plan_update=None,
                error=None,
            )
        return SimpleNamespace(
            assistant_message="Got it. I will record that stack choice.",
            questions=[],
            plan_update={"requirements_append": ["Use React + Tailwind with a dark theme."]},
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlan the admin dashboard\nReact + Tailwind\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 2
    assert "Planner update ignored because this message did not look planning-related" not in (
        result.output
    )
    plan_ref = captured["plan_ref"] or {}
    assert plan_ref.get("requirements") == ["Use React + Tailwind with a dark theme."]
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "no plan_update proposed" in planner_summary
    assert "added requirements: 1" in planner_summary


def test_forge_planner_failure_falls_back_to_goal_capture_when_goal_empty(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"swarm_calls": [], "plan_ref": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["plan_ref"] = forge_state.plan
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            assistant_message="Planner assistant JSON did not match schema; no plan updates were applied.",
            questions=[],
            plan_update=None,
            error="schema_mismatch",
        )

    def fake_run_swarm(**kwargs: Any) -> int:
        captured["swarm_calls"].append(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nImplement robust planner parsing\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert (
        "Captured project goal because the planner produced no structured update." in result.output
    )
    plan_ref = captured["plan_ref"] or {}
    assert plan_ref.get("project_goal") == "Implement robust planner parsing"
    assert plan_ref.get("summary") == "Implement robust planner parsing"
    assert plan_ref.get("requirements") == []
    assert plan_ref.get("tasks") == []
    assert "Plan is empty." in result.output
    assert len(captured["swarm_calls"]) == 0


def test_forge_error_fallback_sets_empty_goal_without_local_small_talk_gate(
    tmp_path: Path,
) -> None:
    plan = {
        "run_id": "wf",
        "project_goal": "",
        "summary": "",
        "requirements": [],
        "tasks": [],
        "assets": [],
    }
    plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
    notes_dir = plan_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    paths = SimpleNamespace(
        run_id="wf",
        plan_json_path=plan_dir / "plan.json",
        plan_md_path=plan_dir / "PLAN.md",
        notes_dir=notes_dir,
        notes_path=notes_dir / "user_notes.md",
    )
    console = Console(width=120, record=True, color_system=None, force_terminal=False)

    captured = cli_mod._capture_forge_requirement_from_planner_fallback(
        plan=plan,
        paths=paths,
        console=console,
        user_text="hello",
    )

    assert captured is True
    assert plan["project_goal"] == "hello"
    assert plan["requirements"] == []
    rendered = console.export_text()
    assert "Captured project goal" in rendered


def test_forge_error_fallback_sets_empty_goal_without_local_meta_gate(tmp_path: Path) -> None:
    plan = {
        "run_id": "wf",
        "project_goal": "",
        "summary": "",
        "requirements": [],
        "tasks": [],
        "assets": [],
    }
    plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
    notes_dir = plan_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    paths = SimpleNamespace(
        run_id="wf",
        plan_json_path=plan_dir / "plan.json",
        plan_md_path=plan_dir / "PLAN.md",
        notes_dir=notes_dir,
        notes_path=notes_dir / "user_notes.md",
    )
    console = Console(width=120, record=True, color_system=None, force_terminal=False)

    captured = cli_mod._capture_forge_requirement_from_planner_fallback(
        plan=plan,
        paths=paths,
        console=console,
        user_text="what's your name?",
    )

    assert captured is True
    assert plan["project_goal"] == "what's your name?"
    assert plan["requirements"] == []
    rendered = console.export_text()
    assert "Captured project goal" in rendered


def test_forge_planner_trace_progress_compact(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "progress": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummySurface:
        trace_level = "compact"

        @staticmethod
        def on_progress_update(message: str) -> None:
            captured["progress"].append(message)

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        surface = _DummySurface()
        client = _DummyClient()
        stream = True
        mode = "review"
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        on_text_delta = kwargs.get("on_text_delta")
        if callable(on_text_delta):
            on_text_delta("planner-delta")
        return SimpleNamespace(
            assistant_message="Planner conversational response.",
            questions=[],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\n/trace compact\nPlan API boundary and auth tasks\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 1
    planner_call = captured["planner_calls"][0]
    assert planner_call.get("stream") is True
    assert callable(planner_call.get("on_text_delta"))
    assert any("Planner assistant is analyzing your request." in p for p in captured["progress"])
    assert any("Receiving planner output..." in p for p in captured["progress"])
    assert any("Planner response ready." in p for p in captured["progress"])


def test_trace_compact_uses_truthful_planner_error_trace_and_summary(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "progress": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummySurface:
        trace_level = "compact"

        @staticmethod
        def on_progress_update(message: str) -> None:
            captured["progress"].append(message)

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        surface = _DummySurface()
        client = _DummyClient()
        stream = True
        mode = "review"
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        return SimpleNamespace(
            assistant_message="Planner assistant returned no safe structured update.",
            questions=[],
            plan_update=None,
            error="empty_response",
            request_retry_count=1,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\n/trace compact\nPlan API boundary and auth tasks\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 1
    assert not any("Planner response ready." in p for p in captured["progress"])
    assert any(
        "Planner returned an error after 1 transient retry; using fallback handling." in p
        for p in captured["progress"]
    )

    plan_dir = tmp_path / ".alysis" / "runs" / "wf" / "plan"
    planner_summary = (plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "planner error after 1 transient retry: empty_response" in planner_summary
    assert "no plan_update proposed" not in planner_summary

    notes_text = (plan_dir / "notes" / "user_notes.md").read_text(encoding="utf-8")
    assert "Planner error after 1 transient retry: empty_response" in notes_text
    assert "Captured project goal (planner produced no structured update)." in notes_text


def test_forge_trace_off_disables_planner_trace_and_streaming(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "progress": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummySurface:
        trace_level = "compact"

        @staticmethod
        def on_progress_update(message: str) -> None:
            captured["progress"].append(message)

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        surface = _DummySurface()
        client = _DummyClient()
        stream = True
        mode = "review"
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        return SimpleNamespace(
            assistant_message="Planner response with trace off.",
            questions=[],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\n/trace off\nPlan migration and tests\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 1
    planner_call = captured["planner_calls"][0]
    assert planner_call.get("stream") is False
    assert planner_call.get("on_text_delta") is None
    assert captured["progress"] == []


def test_forge_execute_plan_enrichment_default_off_in_non_interactive(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": 0}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_planner_turn(**_kwargs: Any) -> Any:
        captured["planner_calls"] += 1
        return SimpleNamespace(
            assistant_message="enrich",
            questions=[],
            plan_update=None,
            error=None,
        )

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(cli_mod, "run_swarm", lambda **_kwargs: 0)
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\nImplement feature X\n/execute plan\n/back\nexit\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["planner_calls"] == 0
    assert "Execution blocked" in result.output


def test_forge_execute_plan_enrichment_triggers_for_runnable_scope_warning() -> None:
    assert cli_mod._forge_should_try_enrichment(
        [f"Task T01 (Fix login bug): {cli_mod.EXECUTION_UNREADY_SCOPE_WARNING}"]
    )


def test_forge_execute_plan_enrichment_enabled_by_env_calls_planner(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "swarm_calls": [], "knowledge_calls": []}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = _forge_run_paths(tmp_path)
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        return SimpleNamespace(
            assistant_message="Enrichment ready",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "acceptance_criteria": ["Feature works with automated tests"],
                        "estimated_files": ["src/alysis_code/cli.py"],
                    }
                ]
            },
            error=None,
        )

    def fake_prepare_planner_knowledge(**kwargs: Any) -> Any:
        captured["knowledge_calls"].append(kwargs)

        class _StubKnowledge:
            @staticmethod
            def render_prompt_section(*, workspace_root: Path) -> str:
                return f"## Relevant Knowledge\n- workspace_root: {workspace_root}"

        return _StubKnowledge()

    def fake_run_swarm(**kwargs: Any) -> int:
        captured["swarm_calls"].append(kwargs)
        tasks = (kwargs.get("plan") or {}).get("tasks") or []
        assert tasks
        task_1 = tasks[0]
        assert task_1.get("acceptance_criteria")
        assert task_1.get("estimated_files")
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "prepare_planner_knowledge", fake_prepare_planner_knowledge)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    env = _env(tmp_path)
    env["ALYSIS_FORGE_ENRICH_PLAN"] = "1"
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\nImplement feature X\n/execute plan\n/back\nexit\n",
        env=env,
    )
    assert result.exit_code == 0
    assert len(captured["knowledge_calls"]) == 1
    assert len(captured["planner_calls"]) == 1
    knowledge_user_text = str(captured["knowledge_calls"][0].get("user_text") or "")
    planner_user_text = str(captured["planner_calls"][0].get("user_text") or "")
    assert knowledge_user_text == planner_user_text
    assert "Enrich the existing plan for execution" in planner_user_text
    assert "missing acceptance_criteria and estimated_files" in planner_user_text
    assert "do not change project_goal, summary, requirements, tasks_add, or tasks_remove" in (
        planner_user_text
    )
    workspace_context = captured["planner_calls"][0].get("workspace_context")
    assert isinstance(workspace_context, dict)
    assert workspace_context.get("focus_relpath") == "."
    assert len(captured["swarm_calls"]) == 1


def test_forge_execute_plan_enrichment_preserves_retry_context_on_final_error(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "swarm_calls": [], "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        }
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        return SimpleNamespace(
            assistant_message="Planner enrichment failed safely.",
            questions=[],
            plan_update=None,
            error="empty_response",
            request_retry_count=1,
        )

    def fake_run_swarm(**kwargs: Any) -> int:
        captured["swarm_calls"].append(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    env = _env(tmp_path)
    env["ALYSIS_FORGE_ENRICH_PLAN"] = "1"
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\nImplement feature X\n/execute plan\n/back\nexit\n",
        env=env,
    )

    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 1
    assert len(captured["swarm_calls"]) == 0
    assert "Plan enrichment: Planner request failed after 1 transient retry." in result.output
    assert "Plan enrichment: Final planner error: empty_response" in result.output
    assert "Execution blocked" in result.output

    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "enrichment skipped after 1 transient retry: empty_response" in planner_summary
    assert "enrichment skipped: empty_response" not in planner_summary

    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "Plan enrichment error after 1 transient retry: empty_response" in notes_text


@pytest.mark.parametrize("trace_full", [False, True])
def test_forge_execute_plan_enrichment_keeps_protected_history_immutable(
    tmp_path: Path, monkeypatch, trace_full: bool
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "swarm_calls": [], "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        surface = SimpleNamespace(trace_level="compact")
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship login safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship login",
                    "description": "Released in src/login.py.",
                    "acceptance_criteria": ["Login shipped"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                },
                {
                    "id": "T02",
                    "title": "Document login rollout",
                    "description": "Planned follow-up",
                    "acceptance_criteria": [],
                    "dependencies": ["T01"],
                    "estimated_files": [],
                    "write_scope": [],
                    "branch": "",
                    "status": "planned",
                    "attempts": 0,
                },
            ],
            "assets": [],
        }
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        return SimpleNamespace(
            assistant_message="Enrichment ready",
            questions=[],
            plan_update={
                "tasks_update": [
                    {"id": "T01", "acceptance_criteria": ["Rewrite shipped task history"]},
                    {
                        "id": "T02",
                        "acceptance_criteria": ["Rollout docs are updated"],
                        "estimated_files": ["README.md", "src/login.py"],
                        "write_scope": ["README.md", "src/login.py"],
                    },
                ]
            },
            error=None,
        )

    def fake_run_swarm(**kwargs: Any) -> int:
        captured["swarm_calls"].append(kwargs)
        tasks = (kwargs.get("plan") or {}).get("tasks") or []
        assert tasks[0]["acceptance_criteria"] == ["Login shipped"]
        assert tasks[0]["estimated_files"] == []
        assert tasks[0]["write_scope"] == []
        assert tasks[1]["acceptance_criteria"] == ["Rollout docs are updated"]
        assert tasks[1]["estimated_files"] == ["README.md", "src/login.py"]
        assert tasks[1]["write_scope"] == ["README.md", "src/login.py"]
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    env = _env(tmp_path)
    env["ALYSIS_FORGE_ENRICH_PLAN"] = "1"
    input_text = (
        "/trace full\n" if trace_full else ""
    ) + "/forge\nImplement feature X\n/execute plan\n/back\nexit\n"
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input=input_text,
        env=env,
    )
    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 1
    assert len(captured["swarm_calls"]) == 1
    warning_line = (
        "Plan enrichment: Ignored plan enrichment acceptance_criteria update for T01 "
        "because that field is already populated."
    )
    assert "Refined plan." in result.output
    if trace_full:
        assert "Reasoning trace set for this session: full" in result.output
        assert warning_line in result.output
    else:
        assert warning_line not in result.output
        assert "Plan enrichment: Ignored" not in result.output
    paths = captured["paths"]
    assert paths is not None
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    if trace_full:
        assert "Plan enrichment warning:" in notes_text
        assert (
            "Ignored plan enrichment acceptance_criteria update for T01 because that field is already populated."
            in notes_text
        )
    else:
        assert "Plan enrichment warning:" not in notes_text
        assert "Ignored plan enrichment" not in notes_text


def test_forge_execute_plan_enrichment_strips_protected_update_only_follow_up(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {"planner_calls": [], "swarm_calls": [], "paths": None}

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        yes = False
        max_steps = 25
        cfg = AppConfig(model="test-model")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_enter_forge_mode(
        *,
        root: Path,
        console: Console,
        forge_state: Any,
        model: str | None = None,
        mode: str | None = None,
    ) -> bool:
        _ = root, console
        paths = _forge_run_paths(tmp_path)
        forge_state.ui_mode = "forge"
        forge_state.paths = paths
        forge_state.plan = {
            "run_id": "wf",
            "project_goal": "Ship slugify safely",
            "summary": "Track shipped work and follow-ups.",
            "requirements": [],
            "tasks": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Released in src/slugify.js.",
                    "acceptance_criteria": ["Slugify shipped"],
                    "dependencies": [],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "done",
                    "attempts": 1,
                },
                {
                    "id": "T02",
                    "title": "Implement slugify rollout follow-up",
                    "description": "Planned follow-up",
                    "acceptance_criteria": [],
                    "dependencies": ["T99"],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                    "branch": "",
                    "status": "planned",
                    "attempts": 0,
                },
            ],
            "assets": [],
        }
        captured["paths"] = paths
        return True

    def fake_run_planner_turn(**kwargs: Any) -> Any:
        captured["planner_calls"].append(kwargs)
        return SimpleNamespace(
            assistant_message="Enrichment ready",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": "Add slugify follow-up tests",
                        "description": "Track post-release slugify tests separately.",
                        "acceptance_criteria": ["Slugify follow-up tests exist"],
                        "dependencies": ["T01"],
                        "estimated_files": ["test/slugify.test.js"],
                        "write_scope": ["test/slugify.test.js"],
                    }
                ]
            },
            error=None,
        )

    def fake_run_swarm(**kwargs: Any) -> int:
        captured["swarm_calls"].append(kwargs)
        tasks = (kwargs.get("plan") or {}).get("tasks") or []
        assert tasks[0]["title"] == "Ship slugify"
        assert tasks[0]["status"] == "done"
        assert tasks[1]["id"] == "T02"
        assert tasks[1]["status"] == "planned"
        assert len(tasks) == 2
        return 0

    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_run_planner_turn)
    monkeypatch.setattr(cli_mod, "run_swarm", fake_run_swarm)
    env = _env(tmp_path)
    env["ALYSIS_FORGE_ENRICH_PLAN"] = "1"
    result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\nImplement feature X\n/execute plan\n/back\nexit\n",
        env=env,
    )

    assert result.exit_code == 0
    assert len(captured["planner_calls"]) == 1
    assert len(captured["swarm_calls"]) == 1
    assert "Plan enrichment:" not in result.output
    assert "Ignored plan enrichment" not in result.output
    assert "Refined plan." not in result.output
    paths = captured["paths"]
    assert paths is not None
    planner_summary = paths.planner_summary_path.read_text(encoding="utf-8")
    assert "enrichment produced no applicable changes" in planner_summary
    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "Plan enrichment warning:" not in notes_text
    assert "Ignored plan enrichment" not in notes_text
    notes_body = "\n".join(line.split("] ", 1)[-1] for line in notes_text.splitlines())
    assert "T03" not in notes_body


def test_forge_execute_plan_enrichment_sanitizer_strips_non_execution_fields() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement API",
                "description": "Pending",
                "acceptance_criteria": [],
                "estimated_files": [],
                "write_scope": [],
            },
            {
                "id": "T02",
                "title": "Ship auth",
                "description": "Already ready",
                "acceptance_criteria": ["Auth works"],
                "estimated_files": ["src/auth.py"],
                "write_scope": ["src/auth.py"],
            },
        ]
    }

    sanitized = cli_mod._sanitize_forge_enrichment_plan_update(
        plan=plan,
        plan_update={
            "project_goal": "Rewrite the plan",
            "summary": "Overwrite everything",
            "requirements_append": ["Do not keep existing requirements"],
            "tasks_add": [{"title": "Sneak in a new task"}],
            "tasks_remove": ["T02"],
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Overwrite title",
                    "description": "Overwrite description",
                    "acceptance_criteria": ["API is covered"],
                    "estimated_files": ["src/api.py"],
                    "write_scope": ["src/api.py"],
                },
                {
                    "id": "T02",
                    "acceptance_criteria": ["Overwrite populated criteria"],
                    "estimated_files": ["README.md"],
                },
            ],
        },
    )

    assert sanitized.plan_update == {
        "tasks_update": [
            {
                "id": "T01",
                "acceptance_criteria": ["API is covered"],
                "estimated_files": ["src/api.py"],
            }
        ]
    }
    warnings_blob = "\n".join(sanitized.warnings)
    assert "project_goal" in warnings_blob
    assert "summary" in warnings_blob
    assert "requirements_append" in warnings_blob
    assert "tasks_add" in warnings_blob
    assert "tasks_remove" in warnings_blob
    assert "title" in warnings_blob
    assert "description" in warnings_blob
    assert "write_scope" in warnings_blob
    assert "T02" in warnings_blob


def test_code_only_turn_ephemeral_system_prompts_do_not_pollute_persisted_history(
    tmp_path: Path,
) -> None:
    internal_prompt = (
        "You are handling one host-injected turn.\n"
        "Do not write files. Do not run shell commands. Planning text only."
    )
    reply = "We were focused on turn understanding."

    class _RepoClient:
        model = "test-model"
        temperature = 0.2

        def chat(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                content=reply,
                usage=None,
                response_model=self.model,
                tool_calls=[],
            )

    session = agent_loop_mod.create_session(
        cfg=AppConfig(model="test-model", routing_mode="auto"),
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
    )
    session.client = _RepoClient()  # type: ignore[assignment]
    session.router_client = None
    try:
        exit_code = session.run_turn(
            "let's work",
            routing_mode_override="code_only",
            ephemeral_system_messages=[internal_prompt],
        )
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    events = list(read_session_events(log_path))
    serialized_events = [json.dumps(event, ensure_ascii=False) for event in events]
    assert all(internal_prompt not in item for item in serialized_events)
    assert any(
        str(event.get("type") or "") == "user_message"
        and str((event.get("payload") or {}).get("content") or "") == "let's work"
        for event in events
    )
    assert any(
        str(event.get("type") or "") == "assistant_message"
        and str((event.get("payload") or {}).get("content") or "") == reply
        for event in events
    )
