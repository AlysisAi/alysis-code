"""Tests for the Forge post-execution completion report and its delivery paths.

Covers: the deterministic report builder (``forge_completion.py``), the grounded
run-hint detection, the ``/execute plan`` handler wiring (classic print vs TUI
sink), the TUI worker finalize logic (a soft-interrupt must never silently eat
a finished run's output), and the swarm-trace ``task_id=None`` normalization.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from alysis_code import cli as cli_mod
from alysis_code.cli_impl import chat as chat_impl_mod
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run, load_plan, save_plan
from alysis_code.forge_completion import (
    build_forge_completion_report,
    detect_run_hints,
)
from alysis_code.swarm_trace import build_swarm_trace_event


def _report_paths(tmp_path: Path) -> SimpleNamespace:
    execution_dir = tmp_path / ".alysis" / "runs" / "wf" / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        root=tmp_path,
        execution_dir=execution_dir,
        execution_reports_dir=execution_dir / "reports",
    )


def _write_worker_result(paths: SimpleNamespace, payload: dict[str, Any]) -> None:
    results_dir = Path(paths.execution_dir) / "worker_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(payload["task_id"])
    (results_dir / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------- report builder


def test_report_success_lists_files_where_and_static_site_hint(tmp_path: Path) -> None:
    paths = _report_paths(tmp_path)
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    _write_worker_result(
        paths,
        {
            "task_id": "T01",
            "success": True,
            "changed_files": ["site/index.html", "site/styles.css"],
            "verify_failed": False,
        },
    )
    plan = {"tasks": [{"id": "T01", "title": "Build landing page", "status": "done"}]}

    report = build_forge_completion_report(
        paths=paths, plan=plan, run_status="clean", run_clean=True, exit_code=0
    )

    assert "Forge execution complete — 1/1 tasks finished" in report
    assert "Where the work landed" in report
    assert str(tmp_path) in report
    assert "T01 · Build landing page" in report
    assert "2 files changed" in report
    assert "`site/index.html`" in report
    assert "Try it:" in report
    assert "python -m http.server 8000" in report
    assert str(site_dir) in report
    assert "swarm_summary.md" in report


def test_report_includes_worker_note_from_knowledge_capture(tmp_path: Path) -> None:
    paths = _report_paths(tmp_path)
    capture_dir = Path(paths.execution_dir) / "knowledge_capture" / "T01" / "ts"
    capture_dir.mkdir(parents=True)
    (capture_dir / "assistant_message.md").write_text(
        "# Heading skipped\n\nBuilt the landing page with a responsive hero.\nMore text.\n",
        encoding="utf-8",
    )
    _write_worker_result(
        paths,
        {
            "task_id": "T01",
            "success": True,
            "changed_files": ["index.html"],
            "verify_failed": False,
            "knowledge_capture_artifact_dir": str(capture_dir),
        },
    )
    plan = {"tasks": [{"id": "T01", "title": "Build page", "status": "done"}]}

    report = build_forge_completion_report(paths=paths, plan=plan, exit_code=0)

    assert "Built the landing page with a responsive hero." in report
    assert "Heading skipped" not in report


def test_report_warns_when_no_files_changed(tmp_path: Path) -> None:
    paths = _report_paths(tmp_path)
    plan = {
        "tasks": [
            {"id": "T01", "title": "Build site", "status": "already_satisfied"},
            {"id": "T02", "title": "Style site", "status": "already_satisfied"},
        ]
    }

    report = build_forge_completion_report(paths=paths, plan=plan, exit_code=0)

    assert "No files were changed by this run." in report
    assert "already satisfied" in report
    assert "Try it:" not in report


def test_report_failed_task_shows_reason_and_issue_headline(tmp_path: Path) -> None:
    paths = _report_paths(tmp_path)
    _write_worker_result(
        paths,
        {
            "task_id": "T02",
            "success": False,
            "changed_files": [],
            "verify_failed": False,
            "failure_reason": "verification command exited 1",
        },
    )
    plan = {
        "tasks": [
            {"id": "T01", "title": "Build page", "status": "done"},
            {"id": "T02", "title": "Deploy", "status": "failed"},
            {"id": "T03", "title": "Polish", "status": "planned"},
        ]
    }

    report = build_forge_completion_report(paths=paths, plan=plan, exit_code=1)

    assert "Forge execution finished with issues" in report
    assert "1 finished · 1 failed · 1 not finished" in report
    assert "verification command exited 1" in report
    assert "✗" in report


def test_report_flags_warn_tolerated_verification(tmp_path: Path) -> None:
    paths = _report_paths(tmp_path)
    _write_worker_result(
        paths,
        {
            "task_id": "T01",
            "success": True,
            "changed_files": ["app.py"],
            "verify_failed": True,
        },
    )
    plan = {"tasks": [{"id": "T01", "title": "Build app", "status": "done"}]}

    report = build_forge_completion_report(
        paths=paths,
        plan=plan,
        run_status="completed_with_verification_warnings",
        run_clean=False,
        exit_code=0,
    )

    assert "completed_with_verification_warnings" in report
    assert "checks failed but were tolerated" in report


# ------------------------------------------------------------------- run hints


def test_detect_run_hints_prefers_changed_index_html_dir(tmp_path: Path) -> None:
    nested = tmp_path / "website"
    nested.mkdir()
    (nested / "index.html").write_text("<html></html>", encoding="utf-8")

    hints = detect_run_hints(tmp_path, ["website/index.html"])

    assert len(hints) == 1
    assert str(nested) in hints[0]
    assert "http://localhost:8000" in hints[0]


def test_detect_run_hints_package_json_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )
    hints = detect_run_hints(tmp_path, ["package.json"])
    assert any("npm install && npm run dev" in hint for hint in hints)

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "node server.js"}}), encoding="utf-8"
    )
    hints = detect_run_hints(tmp_path, ["package.json"])
    assert any("npm start" in hint and "npm install" not in hint for hint in hints)


def test_detect_run_hints_nothing_grounded(tmp_path: Path) -> None:
    assert detect_run_hints(tmp_path, ["src/app.py"]) == []
    assert detect_run_hints(tmp_path, ["missing/index.html"]) == []


# ------------------------------------------------------- /execute handler wiring


def _execution_ready_plan_task() -> dict[str, Any]:
    return {
        "id": "T01",
        "title": "Update auth flow",
        "description": "Update the auth flow implementation code.",
        "acceptance_criteria": ["Login works after the update."],
        "dependencies": [],
        "estimated_files": ["src/auth.py"],
        "write_scope": ["src/auth.py"],
        "branch": "",
        "status": "planned",
        "attempts": 0,
    }


def _execute_session() -> SimpleNamespace:
    return SimpleNamespace(
        cfg=AppConfig(model="test-model"),
        client=SimpleNamespace(api_key="k", model="test-model", temperature=1.0),
        store=SimpleNamespace(enabled=False),
        yes=False,
        stream=False,
    )


def _fake_run_swarm_writing_site(**kwargs: Any) -> int:
    plan = kwargs["plan"]
    paths = kwargs["paths"]
    for task in plan["tasks"]:
        task["status"] = "done"
    save_plan(paths, plan)
    site_dir = Path(paths.root) / "site"
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    results_dir = Path(paths.execution_dir) / "worker_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "T01.json").write_text(
        json.dumps(
            {
                "task_id": "T01",
                "success": True,
                "changed_files": ["site/index.html"],
                "verify_failed": False,
            }
        ),
        encoding="utf-8",
    )
    (Path(paths.execution_dir) / "swarm_summary.json").write_text(
        json.dumps({"status": "clean", "clean": True}), encoding="utf-8"
    )
    return 0


def _run_execute_plan(
    tmp_path: Path,
    monkeypatch,
    *,
    console: Any,
    report_sink: Any | None,
) -> tuple[Any, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["tasks"].append(_execution_ready_plan_task())
    save_plan(paths, plan)

    monkeypatch.setattr(cli_mod, "run_swarm", _fake_run_swarm_writing_site)
    chat_impl_mod._sync_cli_globals(cli_mod)

    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )
    forge_state.workspace_context = {"stale": True}

    kwargs: dict[str, Any] = {}
    if report_sink is not None:
        kwargs["forge_execution_report_sink"] = report_sink
    result = chat_impl_mod._handle_forge_chat_command(
        input_text="/execute plan",
        forge_state=forge_state,
        session=_execute_session(),
        console=console,
        **kwargs,
    )
    assert result == "handled"
    return paths, forge_state


def test_execute_plan_prints_completion_report_classic(tmp_path: Path, monkeypatch) -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, no_color=True, width=120)

    paths, forge_state = _run_execute_plan(tmp_path, monkeypatch, console=console, report_sink=None)

    output = buffer.getvalue()
    assert "Execution complete" in output
    assert "Forge execution complete" in output
    assert "Where the work landed" in output
    assert "index.html" in output
    assert "http.server" in output
    report_artifact = Path(paths.execution_dir) / "completion_report.md"
    assert report_artifact.exists()
    assert "Try it:" in report_artifact.read_text(encoding="utf-8")
    assert forge_state.swarm_run_attempted is True
    # The run rewrote the workspace: the cached pre-execution scan must be
    # dropped so follow-up planner questions see what now exists on disk.
    assert forge_state.workspace_context is None


def test_execute_plan_routes_report_to_sink_in_tui(tmp_path: Path, monkeypatch) -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, no_color=True, width=120)
    reports: list[str] = []

    _paths, forge_state = _run_execute_plan(
        tmp_path, monkeypatch, console=console, report_sink=reports.append
    )

    assert len(reports) == 1
    assert "Forge execution complete" in reports[0]
    assert "python -m http.server 8000" in reports[0]
    # Sink mode must not double-print the report into the captured console text.
    assert "Try it:" not in buffer.getvalue()
    assert forge_state.swarm_run_attempted is True


def test_execute_plan_rejection_leaves_run_attempted_false(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    monkeypatch.setattr(
        cli_mod,
        "run_swarm",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    chat_impl_mod._sync_cli_globals(cli_mod)

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, no_color=True, width=120)
    forge_state = cli_mod._ForgeChatState(
        ui_mode="forge",
        paths=paths,
        plan=load_plan(paths),
    )
    forge_state.swarm_run_attempted = True  # stale value from a previous run

    result = chat_impl_mod._handle_forge_chat_command(
        input_text="/execute plan",
        forge_state=forge_state,
        session=_execute_session(),
        console=console,
    )

    assert result == "handled"
    assert forge_state.swarm_run_attempted is False


# ------------------------------------------------------------- TUI finalize path


class _RecordingSurface:
    def __init__(self) -> None:
        self.system_notes: list[str] = []
        self.end_calls: list[dict[str, Any]] = []
        self.interrupts: int = 0

    def append_system(self, text: str) -> None:
        self.system_notes.append(text)

    def end_forge(self, summary: str = "", *, paths: Any | None = None) -> None:
        self.end_calls.append({"summary": summary, "paths": paths})

    def interrupt_forge(self) -> None:
        self.interrupts += 1


def _finalize(**overrides: Any) -> _RecordingSurface:
    surface = _RecordingSurface()
    kwargs: dict[str, Any] = {
        "surface": surface,
        "token": SimpleNamespace(is_cancelled=False),
        "paths": SimpleNamespace(run_id="wf"),
        "forge_state": SimpleNamespace(swarm_run_attempted=True),
        "captured": "── DONE ──\nExecution complete",
        "completed": True,
    }
    kwargs.update(overrides)
    chat_impl_mod._finalize_deferred_forge_execution(**kwargs)
    return surface


def test_finalize_normal_completion_appends_output_and_ends() -> None:
    surface = _finalize()
    assert surface.system_notes == ["── DONE ──\nExecution complete"]
    assert surface.interrupts == 0
    assert len(surface.end_calls) == 1
    assert surface.end_calls[0]["summary"] == ""


def test_finalize_keeps_finished_run_output_despite_soft_interrupt() -> None:
    # One Esc at any point flips the one-way token, but run_swarm cannot be
    # stopped — the handler still returns normally. The output must survive
    # and the view must be re-finalized from the run's real paths.
    paths = SimpleNamespace(run_id="wf")
    surface = _finalize(
        token=SimpleNamespace(is_cancelled=True),
        paths=paths,
        completed=True,
    )
    assert surface.system_notes == ["── DONE ──\nExecution complete"]
    assert surface.interrupts == 0
    assert len(surface.end_calls) == 1
    assert surface.end_calls[0]["paths"] is paths


def test_finalize_interrupted_crash_drops_output_and_freezes_interrupted() -> None:
    surface = _finalize(
        token=SimpleNamespace(is_cancelled=True),
        completed=False,
    )
    assert surface.system_notes == []
    assert surface.interrupts == 1
    assert surface.end_calls == []


def test_finalize_rejected_execute_reports_not_started() -> None:
    surface = _finalize(
        forge_state=SimpleNamespace(swarm_run_attempted=False),
        captured="Forge: plan is empty.",
        completed=True,
    )
    assert surface.system_notes == ["Forge: plan is empty."]
    assert len(surface.end_calls) == 1
    assert "did not start" in surface.end_calls[0]["summary"]


# ----------------------------------------------------------------- trace task_id


def test_swarm_trace_event_normalizes_missing_task_id() -> None:
    assert build_swarm_trace_event(run_id="r", phase="p", message="m", task_id=None).task_id is None
    assert build_swarm_trace_event(run_id="r", phase="p", message="m", task_id="  ").task_id is None
    assert build_swarm_trace_event(run_id="r", phase="p", message="m", task_id="T1").task_id == "T1"
