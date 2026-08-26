"""Strict write-scope triage: adjacent changes amend, protected and unrelated ones block."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.forge_events import EVENT_NAMES, EVENT_SCOPE_AMENDED
from alysis_code.plan_validation import (
    SCOPE_GLOB_PREFERENCE_GUIDANCE,
    find_plan_acceptance_issues,
)
from alysis_code.task_scope import (
    SCOPE_ADJACENT_GENERATED_ARTIFACT,
    SCOPE_ADJACENT_NEW_FILE_IN_SCOPE_DIR,
    SCOPE_ADJACENT_SIBLING_TEST,
    SCOPE_CLASS_ADJACENT,
    SCOPE_CLASS_DANGEROUS_UNRELATED,
    SCOPE_CLASS_LIKELY_MISSING_SCOPE,
    SCOPE_CLASS_PROTECTED,
    SCOPE_TRIAGE_ADJACENT,
    SCOPE_TRIAGE_PROTECTED,
    SCOPE_TRIAGE_UNRELATED,
    ScopeAmendment,
    apply_scope_amendments,
    assess_scope_changes,
    declared_scope_directories,
    describe_scope_violations,
    is_protected_scope_path,
    suggested_scope_pattern_for,
)

# ---------------------------------------------------------------------------
# classification: adjacent
# ---------------------------------------------------------------------------


def _task(write_scope: list[str]) -> dict[str, object]:
    return {
        "id": "T01",
        "title": "Implement feature slice",
        "write_scope": list(write_scope),
        "estimated_files": list(write_scope),
    }


def _diagnostic_for(assessment, path: str):
    return next(item for item in assessment.diagnostics if item.path == path)


def test_sibling_test_file_is_adjacent_and_amends_scope(tmp_path: Path) -> None:
    task = _task(["src/mod.py"])

    assessment = assess_scope_changes(
        ["src/mod.py", "tests/test_mod.py"],
        ["src/mod.py"],
        task=task,
        root=tmp_path,
        amend_adjacent=True,
        new_paths=["tests/test_mod.py"],
    )

    assert assessment.ok is True
    assert assessment.blocking_paths == []
    assert assessment.in_scope_paths == ["src/mod.py"]
    assert assessment.adjacent_paths == ["tests/test_mod.py"]
    amendment = assessment.amendments[0]
    assert amendment.path == "tests/test_mod.py"
    assert amendment.pattern == "tests/test_mod.py"
    assert amendment.reason_code == SCOPE_ADJACENT_SIBLING_TEST
    assert amendment.source_path == "src/mod.py"
    assert amendment.suggested_pattern == "tests/**"
    diagnostic = _diagnostic_for(assessment, "tests/test_mod.py")
    assert diagnostic.classification == SCOPE_CLASS_ADJACENT
    assert diagnostic.triage == SCOPE_TRIAGE_ADJACENT
    assert diagnostic.allowed is True


def test_sibling_test_file_naming_conventions_are_recognized(tmp_path: Path) -> None:
    for changed in ("src/mod_test.py", "src/mod.test.ts", "tests/mod_spec.rb"):
        assessment = assess_scope_changes(
            [changed],
            ["src/mod.py"],
            task=_task(["src/mod.py"]),
            root=tmp_path,
            amend_adjacent=True,
        )
        assert assessment.ok is True, changed
        assert assessment.amendments[0].reason_code == SCOPE_ADJACENT_SIBLING_TEST, changed


def test_new_file_in_declared_directory_is_adjacent(tmp_path: Path) -> None:
    assessment = assess_scope_changes(
        ["src/pkg/helper.py"],
        ["src/pkg/app.py"],
        task=_task(["src/pkg/app.py"]),
        root=tmp_path,
        amend_adjacent=True,
        new_paths=["src/pkg/helper.py"],
    )

    assert assessment.ok is True
    amendment = assessment.amendments[0]
    assert amendment.reason_code == SCOPE_ADJACENT_NEW_FILE_IN_SCOPE_DIR
    assert amendment.suggested_pattern == "src/pkg/**"


def test_new_file_under_glob_scope_prefix_is_adjacent(tmp_path: Path) -> None:
    # The glob itself does not match the new file, but its static prefix names a directory
    # the scope already reaches into.
    assessment = assess_scope_changes(
        ["src/pkg/helper.py"],
        ["src/pkg/*.md"],
        task=_task(["src/pkg/*.md"]),
        root=tmp_path,
        amend_adjacent=True,
        new_paths=["src/pkg/helper.py"],
    )

    assert assessment.ok is True
    assert assessment.amendments[0].reason_code == SCOPE_ADJACENT_NEW_FILE_IN_SCOPE_DIR


def test_edited_existing_neighbour_is_not_adjacent_when_creation_is_known(
    tmp_path: Path,
) -> None:
    # The neighbour already existed, so the task edited it rather than creating it: that is
    # a scope question the plan has to answer, not a bookkeeping gap to auto-amend.
    assessment = assess_scope_changes(
        ["src/pkg/database.py"],
        ["src/pkg/app.py"],
        task=_task(["src/pkg/app.py"]),
        root=tmp_path,
        amend_adjacent=True,
        new_paths=[],
    )

    assert assessment.ok is False
    assert assessment.blocking_paths == ["src/pkg/database.py"]
    assert _diagnostic_for(assessment, "src/pkg/database.py").classification == (
        SCOPE_CLASS_LIKELY_MISSING_SCOPE
    )


def test_generated_sibling_artifact_is_adjacent(tmp_path: Path) -> None:
    assessment = assess_scope_changes(
        ["src/schema.generated.ts"],
        ["src/schema.ts"],
        task=_task(["src/schema.ts"]),
        root=tmp_path,
        amend_adjacent=True,
    )

    assert assessment.ok is True
    assert assessment.amendments[0].reason_code == SCOPE_ADJACENT_GENERATED_ARTIFACT
    assert assessment.amendments[0].source_path == "src/schema.ts"


def test_wrong_package_manager_lockfile_stays_blocked(tmp_path: Path) -> None:
    # package-lock.json is a managed lockfile: whether it belongs to package.json is the
    # companion rules' call, and for a pnpm project the answer is no.
    (tmp_path / "package.json").write_text('{"packageManager":"pnpm@9.0.0"}\n', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")

    assessment = assess_scope_changes(
        ["package-lock.json"],
        ["package.json"],
        task=_task(["package.json"]),
        root=tmp_path,
        amend_adjacent=True,
    )

    assert assessment.ok is False
    assert assessment.amendments == []


# ---------------------------------------------------------------------------
# classification: protected
# ---------------------------------------------------------------------------


def test_protected_paths_always_block_even_with_amendment_enabled(tmp_path: Path) -> None:
    assessment = assess_scope_changes(
        [".forge/state.json"],
        ["src/mod.py"],
        task=_task(["src/mod.py"]),
        root=tmp_path,
        amend_adjacent=True,
        new_paths=[".forge/state.json"],
    )

    assert assessment.ok is False
    assert assessment.blocking_paths == [".forge/state.json"]
    diagnostic = _diagnostic_for(assessment, ".forge/state.json")
    assert diagnostic.classification == SCOPE_CLASS_PROTECTED
    assert diagnostic.triage == SCOPE_TRIAGE_PROTECTED
    assert diagnostic.recommended_action == "reject_hard"
    # No scope patch may legitimise a protected path.
    assert assessment.suggested_scope_patterns == []
    assert assessment.protected_paths == [".forge/state.json"]


def test_workspace_escaping_paths_are_protected(tmp_path: Path) -> None:
    assessment = assess_scope_changes(
        ["../outside.py"],
        ["src/mod.py"],
        task=_task(["src/mod.py"]),
        root=tmp_path,
        amend_adjacent=True,
    )

    assert assessment.ok is False
    assert _diagnostic_for(assessment, "../outside.py").classification == SCOPE_CLASS_PROTECTED


def test_is_protected_scope_path_covers_internal_and_vcs_paths() -> None:
    assert is_protected_scope_path(".git/config") is True
    assert is_protected_scope_path(".alysis/state.json") is True
    assert is_protected_scope_path(".forge/run.json") is True
    assert is_protected_scope_path("C:/Windows/system32/hosts") is True
    assert is_protected_scope_path("/etc/passwd") is True
    assert is_protected_scope_path("src/mod.py") is False


# ---------------------------------------------------------------------------
# classification: unrelated
# ---------------------------------------------------------------------------


def test_unrelated_change_blocks_with_classified_list_and_suggested_patterns(
    tmp_path: Path,
) -> None:
    assessment = assess_scope_changes(
        ["src/mod.py", "billing/invoices.py", "settings.py"],
        ["src/mod.py"],
        task=_task(["src/mod.py"]),
        root=tmp_path,
        amend_adjacent=True,
        new_paths=["billing/invoices.py"],
    )

    assert assessment.ok is False
    assert assessment.blocking_paths == ["billing/invoices.py", "settings.py"]
    assert assessment.unrelated_paths == ["billing/invoices.py", "settings.py"]
    assert _diagnostic_for(assessment, "billing/invoices.py").triage == SCOPE_TRIAGE_UNRELATED
    assert _diagnostic_for(assessment, "settings.py").classification == (
        SCOPE_CLASS_DANGEROUS_UNRELATED
    )
    assert assessment.suggested_scope_patterns == ["billing/**", "settings.py"]
    lines = describe_scope_violations(assessment.diagnostics)
    assert any(line.startswith("billing/invoices.py (unrelated/") for line in lines)
    # The in-scope change is not a violation, so it is not listed as one.
    assert not any(line.startswith("src/mod.py (") for line in lines)


def test_amendment_is_opt_in_so_warn_and_off_paths_are_unchanged(tmp_path: Path) -> None:
    changed = ["tests/test_mod.py"]
    allowed = ["src/mod.py"]

    without_amendment = assess_scope_changes(changed, allowed, task=_task(allowed), root=tmp_path)
    with_amendment = assess_scope_changes(
        changed, allowed, task=_task(allowed), root=tmp_path, amend_adjacent=True
    )

    assert without_amendment.ok is False
    assert without_amendment.amendments == []
    assert without_amendment.blocking_paths == ["tests/test_mod.py"]
    assert with_amendment.ok is True


# ---------------------------------------------------------------------------
# amendment plumbing
# ---------------------------------------------------------------------------


def test_apply_scope_amendments_appends_without_duplicating() -> None:
    task: dict[str, object] = {"write_scope": ["src/mod.py"]}
    amendments = [
        ScopeAmendment(
            path="tests/test_mod.py",
            pattern="tests/test_mod.py",
            reason_code=SCOPE_ADJACENT_SIBLING_TEST,
            evidence="sibling test",
        ),
        ScopeAmendment(
            path="src/mod.py",
            pattern="src/mod.py",
            reason_code=SCOPE_ADJACENT_SIBLING_TEST,
            evidence="already declared",
        ),
    ]

    added = apply_scope_amendments(task, amendments)

    assert added == ["tests/test_mod.py"]
    assert task["write_scope"] == ["src/mod.py", "tests/test_mod.py"]


def test_apply_scope_amendments_seeds_missing_write_scope() -> None:
    task: dict[str, object] = {}
    added = apply_scope_amendments(
        task,
        [
            ScopeAmendment(
                path="tests/test_mod.py",
                pattern="tests/test_mod.py",
                reason_code=SCOPE_ADJACENT_SIBLING_TEST,
                evidence="sibling test",
            )
        ],
    )

    assert added == ["tests/test_mod.py"]
    assert task["write_scope"] == ["tests/test_mod.py"]


def test_suggested_pattern_prefers_directory_globs() -> None:
    assert suggested_scope_pattern_for("src/pkg/mod.py") == "src/pkg/**"
    assert suggested_scope_pattern_for("settings.py") == "settings.py"


def test_declared_scope_directories_ignores_repository_wide_globs() -> None:
    assert declared_scope_directories(["src/pkg/app.py", "docs/*.md", "**"]) == {
        "src/pkg",
        "docs",
    }


def test_scope_amended_is_a_declared_machine_event() -> None:
    assert EVENT_SCOPE_AMENDED in EVENT_NAMES


# ---------------------------------------------------------------------------
# planner guidance
# ---------------------------------------------------------------------------


def test_plan_acceptance_scope_issues_teach_directory_globs() -> None:
    plan = {
        "schema_version": 2,
        "project_goal": "Ship the parser",
        "tasks": [
            {
                "id": "T01",
                "title": "Document the parser",
                "description": "Update the parser documentation for src/parser.py.",
                "status": "todo",
                "acceptance_criteria": ["Docs updated."],
                "estimated_files": ["docs/parser.md"],
                "write_scope": ["docs/parser.md"],
            }
        ],
    }

    issues = find_plan_acceptance_issues(plan)

    scope_issues = [issue for issue in issues if issue.rule_id in {"R3", "R4"}]
    assert scope_issues, [issue.rule_id for issue in issues]
    assert all(SCOPE_GLOB_PREFERENCE_GUIDANCE in issue.detail for issue in scope_issues)
    assert "directory-level globs" in SCOPE_GLOB_PREFERENCE_GUIDANCE


def test_missing_write_scope_issue_carries_glob_guidance() -> None:
    plan = {
        "schema_version": 2,
        "project_goal": "Ship the parser",
        "tasks": [
            {
                "id": "T01",
                "title": "Implement the parser in src/parser.py",
                "description": "Add parsing support to src/parser.py.",
                "status": "todo",
                "acceptance_criteria": ["Parser implemented."],
                "estimated_files": [],
                "write_scope": [],
            }
        ],
    }

    issues = find_plan_acceptance_issues(plan)

    missing_scope = [
        issue for issue in issues if issue.rule_id == "R4" and "write_scope" in issue.observed
    ]
    assert missing_scope
    assert SCOPE_GLOB_PREFERENCE_GUIDANCE in missing_scope[0].detail


# ---------------------------------------------------------------------------
# forge exec: end to end through the strict scope gate
# ---------------------------------------------------------------------------


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _prepare_exec_run(repo: Path, *, write_scope: list[str]) -> tuple[Path, dict, str]:
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Execute tasks safely"
    plan["summary"] = "Execute tasks safely"
    task = add_task(
        plan,
        title="Implement feature slice",
        description="Task created from planning chat: Implement feature slice",
        estimated_files=list(write_scope),
    )
    task["write_scope"] = list(write_scope)
    save_plan(paths, plan)
    return paths.plan_json_path, load_plan(paths), str(task["id"])


def _exec_args(task_id: str, repo: Path) -> list[str]:
    return [
        "forge",
        "exec",
        task_id,
        "--path",
        os.fspath(repo),
        "--model",
        "test-model",
        "--api-key",
        "k",
        "--no-log",
    ]


def _report_text(repo: Path, task_id: str) -> str:
    pointer = json.loads((repo / ".alysis" / "current_run.json").read_text(encoding="utf-8"))
    run_dir = repo / pointer["run_path"]
    return (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")


def test_exec_accepts_worker_creating_a_new_sibling_test_file(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("def parse():\n    return None\n", encoding="utf-8")
    plan_path, _plan, task_id = _prepare_exec_run(repo, write_scope=["src/mod.py"])

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        (root / "src" / "mod.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
        tests_dir = root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_mod.py").write_text(
            "from src.mod import parse\n\n\ndef test_parse():\n    assert parse() == 42\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(alysis_app, _exec_args(task_id, repo), env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    final_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    final_task = final_plan["tasks"][0]
    assert final_task["status"] == "done"
    assert "tests/test_mod.py" in final_task["write_scope"]

    report_text = _report_text(repo, task_id)
    assert "Task blocked due to strict scope isolation." not in report_text
    assert "## Scope Amendments" in report_text
    assert "tests/test_mod.py" in report_text
    assert SCOPE_ADJACENT_SIBLING_TEST in report_text


def test_exec_machine_stream_reports_the_scope_amendment(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("def parse():\n    return None\n", encoding="utf-8")
    _plan_path, _plan, task_id = _prepare_exec_run(repo, write_scope=["src/mod.py"])

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        (root / "src" / "mod.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "tests" / "test_mod.py").write_text(
            "def test_parse():\n    pass\n", encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [*_exec_args(task_id, repo), "--machine"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    events = [
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]
    amended = [item for item in events if item.get("event") == EVENT_SCOPE_AMENDED]
    assert len(amended) == 1
    data = amended[0]["data"]
    assert data["task_id"] == task_id
    assert data["added_patterns"] == ["tests/test_mod.py"]
    assert data["adjacent_paths"] == ["tests/test_mod.py"]
    assert data["adjacent_only"] is False
    assert data["amendments"][0]["reason_code"] == SCOPE_ADJACENT_SIBLING_TEST


def test_exec_blocks_worker_editing_an_unrelated_top_level_module(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("def parse():\n    return None\n", encoding="utf-8")
    (repo / "settings.py").write_text("DEBUG = False\n", encoding="utf-8")
    plan_path, _plan, task_id = _prepare_exec_run(repo, write_scope=["src/mod.py"])

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        (root / "src" / "mod.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
        (root / "settings.py").write_text(
            "DEBUG = True\nSECRET_KEY = 'rewritten by an out-of-scope worker'\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(alysis_app, _exec_args(task_id, repo), env=_env(tmp_path))

    assert result.exit_code == 1, result.output
    final_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    final_task = final_plan["tasks"][0]
    assert final_task["status"] == "failed"
    assert final_task["write_scope"] == ["src/mod.py"]

    report_text = _report_text(repo, task_id)
    assert "Task blocked due to strict scope isolation." in report_text
    assert "settings.py" in report_text
    # The block carries the full triage plus a scope patch, so the plan can be fixed once.
    assert "Classified changes:" in report_text
    assert "Suggested write_scope additions: settings.py" in report_text


def test_exec_rejects_a_task_that_changed_nothing_with_a_did_nothing_message(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("def parse():\n    return None\n", encoding="utf-8")
    _plan_path, _plan, task_id = _prepare_exec_run(repo, write_scope=["src/mod.py"])

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)

    result = runner.invoke(alysis_app, _exec_args(task_id, repo), env=_env(tmp_path))

    assert result.exit_code == 1, result.output
    report_text = _report_text(repo, task_id)
    assert "no material file changes were detected" in report_text
    assert "produced no file changes at all" in report_text
    assert "## Scope Amendments" in report_text


def test_exec_accepts_a_task_whose_only_changes_were_adjacent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
    plan_path, _plan, task_id = _prepare_exec_run(repo, write_scope=["src/mod.py"])

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        # The declared file already did the right thing; only its test was missing.
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "tests" / "test_mod.py").write_text(
            "from src.mod import parse\n\n\ndef test_parse():\n    assert parse() == 42\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(alysis_app, _exec_args(task_id, repo), env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    final_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert final_plan["tasks"][0]["status"] == "done"
    report_text = _report_text(repo, task_id)
    assert "no material file changes were detected" not in report_text
    assert "every change was adjacent to the declared scope" in report_text


def test_exec_scope_warn_mode_still_only_warns_and_never_amends(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("def parse():\n    return None\n", encoding="utf-8")
    plan_path, _plan, task_id = _prepare_exec_run(repo, write_scope=["src/mod.py"])

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        (root / "src" / "mod.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "tests" / "test_mod.py").write_text(
            "def test_parse():\n    pass\n", encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [*_exec_args(task_id, repo), "--scope", "warn"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    final_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    # warn mode reports and changes nothing -- including the plan.
    assert final_plan["tasks"][0]["write_scope"] == ["src/mod.py"]
    report_text = _report_text(repo, task_id)
    assert "Out-of-scope file changes detected" in report_text
    assert "Scope amended:" not in report_text


def test_violation_description_and_suggestions_stay_bounded(tmp_path: Path) -> None:
    changed = [f"unrelated/mod_{index:03d}.py" for index in range(40)]

    assessment = assess_scope_changes(
        changed,
        ["src/mod.py"],
        task=_task(["src/mod.py"]),
        root=tmp_path,
        amend_adjacent=True,
    )

    assert assessment.ok is False
    assert len(assessment.blocking_paths) == 40
    lines = describe_scope_violations(assessment.diagnostics)
    assert len(lines) == 21
    assert lines[-1] == "+20 more"
    # Every one of them lives in the same directory, so the patch is a single glob.
    assert assessment.suggested_scope_patterns == ["unrelated/**"]
