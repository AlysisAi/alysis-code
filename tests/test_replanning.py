from __future__ import annotations

import json
from pathlib import Path

import pytest

from alysis_code.config import AppConfig
from alysis_code.failure_category import FailureCategory
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.integration_gate import IntegrationGateResult
from alysis_code.knowledge_base import (
    write_issue_entry_for_task_id,
    write_task_attempt_entry,
)
from alysis_code.plan_assistant import PlannerTurnResult
from alysis_code.plan_validation import PlannerFailedError
from alysis_code.replanning import (
    build_replanning_trigger,
    resolve_replanning_mode,
    run_replanning_attempt,
    validate_replanning_plan_update,
)
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult


def _integration_result(
    *, paths, batch_index: int, passed: bool, summary: str
) -> IntegrationGateResult:
    batch_label = f"batch_{batch_index:03d}"
    artifact_dir = paths.execution_integration_dir / batch_label
    artifact_dir.mkdir(parents=True, exist_ok=True)
    verify_artifact = artifact_dir / "verify.txt"
    verify_artifact.write_text("verify\n", encoding="utf-8")
    commands_path = artifact_dir / "commands.json"
    commands_path.write_text("{}\n", encoding="utf-8")
    stdout_path = artifact_dir / "stdout.txt"
    stdout_path.write_text("stdout\n", encoding="utf-8")
    stderr_path = artifact_dir / "stderr.txt"
    stderr_path.write_text("stderr\n", encoding="utf-8")
    summary_path = artifact_dir / "summary.md"
    summary_path.write_text(summary + "\n", encoding="utf-8")
    result_path = artifact_dir / "result.json"
    result_path.write_text("{}\n", encoding="utf-8")
    return IntegrationGateResult(
        batch_index=batch_index,
        batch_label=batch_label,
        mode="warn",
        command_source="config.verify_commands_fallback",
        commands=("pytest -q",),
        merged_task_ids=("T01",),
        merged_paths=("src/parser.py",),
        verify_result=VerifyRunResult(
            commands=["pytest -q"],
            command_results=[
                VerifyCommandResult(
                    command="pytest -q",
                    exit_code=0 if passed else 1,
                    output=summary,
                    stdout=summary,
                    stderr="",
                )
            ],
            artifact_path=verify_artifact,
        ),
        artifact_dir=artifact_dir,
        result_path=result_path,
        commands_path=commands_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        summary_path=summary_path,
        verify_artifact_path=verify_artifact,
    )


def test_resolve_replanning_mode_uses_cli_or_config() -> None:
    cfg = AppConfig(model="test-model", replanning_mode="suggest")
    assert resolve_replanning_mode(cfg=cfg, replanning_mode=None) == "suggest"
    assert resolve_replanning_mode(cfg=cfg, replanning_mode="apply") == "apply"


def test_build_replanning_trigger_requires_open_integration_issue(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    assert (
        build_replanning_trigger(
            paths=paths,
            integration_mode="warn",
            merged_task_ids=["T01"],
        )
        is None
    )

    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Open integration issue.",
        paths_in_scope=["src/parser.py"],
        related_tasks=["T01"],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:test",
    )
    trigger = build_replanning_trigger(
        paths=paths,
        integration_mode="warn",
        merged_task_ids=["T01"],
    )
    assert trigger is not None
    assert len(trigger.open_integration_issues) == 1


def test_validate_replanning_plan_update_rejects_completed_task_rewrite() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Done",
                "status": "done",
                "dependencies": [],
                "estimated_files": [],
                "write_scope": [],
            },
        ],
        "requirements": [],
    }
    validation, _ = validate_replanning_plan_update(
        plan=plan,
        plan_update={"tasks_update": [{"id": "T01", "title": "Rewrite done task"}]},
    )
    assert validation.valid is False
    assert not any("mutated protected task history: T01" in item for item in validation.errors)
    assert plan["tasks"][0]["title"] == "Done"


def test_validate_replanning_plan_update_rejects_protected_rewrite_with_scope_context() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Done",
                "status": "done",
                "dependencies": [],
                "estimated_files": ["src/parser.py"],
                "write_scope": ["src/parser.py"],
            },
        ],
        "requirements": [],
    }
    validation, apply_preview = validate_replanning_plan_update(
        plan=plan,
        plan_update={"tasks_update": [{"id": "T01", "title": "Rewrite done task"}]},
        latest_user_text="Only work in src/parser.py while following up on the integration issue.",
    )

    assert validation.valid is False
    assert "replanning proposal made no executable plan changes" in validation.errors
    assert apply_preview.added_task_ids == []
    assert apply_preview.updated_task_ids == []
    assert plan["tasks"][0]["title"] == "Done"


def test_validate_replanning_plan_update_recovers_when_safe_additions_are_mixed_in() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Done",
                "status": "done",
                "dependencies": [],
                "estimated_files": [],
            },
            {
                "id": "T02",
                "title": "Update planned implementation",
                "status": "planned",
                "dependencies": [],
                "acceptance_criteria": ["Planned implementation remains runnable."],
                "estimated_files": ["src/planned.py"],
                "write_scope": ["src/planned.py"],
            },
        ],
        "requirements": [],
    }
    validation, apply_preview = validate_replanning_plan_update(
        plan=plan,
        plan_update={
            "tasks_update": [{"id": "T01", "title": "Rewrite done task"}],
            "tasks_add": [
                {
                    "title": "Legitimate follow-up",
                    "description": "Track follow-up work separately.",
                    "acceptance_criteria": ["New task exists"],
                    "dependencies": ["T01"],
                    "estimated_files": ["src/follow_up.py"],
                    "write_scope": ["src/follow_up.py"],
                }
            ],
        },
    )

    assert validation.valid is True
    assert validation.errors == ()
    assert any("protected non-planned task history" in item for item in validation.warnings)
    assert apply_preview.added_task_ids == ["T03"]
    assert apply_preview.updated_task_ids == []


def test_validate_replanning_plan_update_synthesizes_failed_task_follow_up() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix parser",
                "description": "Initial parser fix failed verification.",
                "status": "failed",
                "dependencies": [],
                "acceptance_criteria": ["Parser handles blank lines."],
                "estimated_files": ["src/parser.py"],
                "write_scope": ["src/parser.py"],
            },
        ],
        "requirements": [],
    }

    validation, apply_preview = validate_replanning_plan_update(
        plan=plan,
        plan_update={
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Repair parser blank-line handling",
                    "description": "Add a new follow-up task for blank-line parser behavior.",
                    "acceptance_criteria": ["Blank-line parser behavior passes."],
                    "dependencies": ["T01"],
                    "estimated_files": ["src/parser.py"],
                    "write_scope": ["src/parser.py"],
                }
            ]
        },
    )

    assert validation.valid is True
    assert apply_preview.synthesized_task_ids == ["T02"]
    assert apply_preview.added_task_ids == ["T02"]
    assert any("protected non-planned task history" in item for item in validation.warnings)
    assert any(
        "Dropped protected non-done dependencies for synthesized follow-up task 'T02': T01" in item
        for item in validation.warnings
    )


def test_validate_replanning_plan_update_drops_failed_dependency_from_added_follow_up() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix parser",
                "description": "Initial parser fix failed verification.",
                "status": "failed",
                "dependencies": [],
                "acceptance_criteria": ["Parser handles blank lines."],
                "estimated_files": ["src/parser.py"],
                "write_scope": ["src/parser.py"],
            },
        ],
        "requirements": [],
    }

    validation, apply_preview = validate_replanning_plan_update(
        plan=plan,
        plan_update={
            "tasks_supersede": ["T01"],
            "tasks_add": [
                {
                    "title": "Repair parser blank-line handling",
                    "description": "Patch the parser in src/parser.py.",
                    "acceptance_criteria": ["Parser handles blank lines."],
                    "dependencies": ["T01"],
                    "estimated_files": ["src/parser.py"],
                    "write_scope": ["src/parser.py"],
                }
            ],
        },
    )

    assert validation.valid is True
    assert apply_preview.added_task_ids == ["T02"]
    assert apply_preview.superseded_task_ids == []
    assert any("protected non-planned task history" in item for item in validation.warnings)
    assert any(
        "Dropped protected non-done dependencies for new task 'Repair parser blank-line handling': T01"
        in item
        for item in validation.warnings
    )


def test_validate_replanning_plan_update_applies_latest_direction_context() -> None:
    plan = {
        "requirements": ["Support TOML config", "Expose timeout configuration"],
        "tasks": [
            {
                "id": "T01",
                "title": "Implement TOML settings loader",
                "description": "Load timeout settings from settings.toml.",
                "acceptance_criteria": ["TOML settings load correctly."],
                "dependencies": [],
                "estimated_files": ["src/settings.py"],
                "write_scope": ["src/settings.py"],
                "status": "planned",
            },
        ],
    }

    validation, apply_preview = validate_replanning_plan_update(
        plan=plan,
        plan_update={
            "tasks_add": [
                {
                    "title": "Implement APP_TIMEOUT_SECONDS env var timeout",
                    "description": "Read APP_TIMEOUT_SECONDS in src/settings.py.",
                    "acceptance_criteria": ["APP_TIMEOUT_SECONDS controls timeout."],
                    "dependencies": [],
                    "estimated_files": ["src/settings.py"],
                    "write_scope": ["src/settings.py"],
                }
            ]
        },
        latest_user_text=(
            "drop TOML from the plan entirely; use APP_TIMEOUT_SECONDS env var instead"
        ),
    )

    assert validation.valid is True
    assert apply_preview.added_task_ids == ["T02"]
    assert apply_preview.superseded_task_ids == ["T01"]
    assert apply_preview.superseded_requirements == ["Support TOML config"]
    assert any(
        "Superseded obsolete planned task 'T01'" in warning for warning in validation.warnings
    )
    assert plan["tasks"][0]["status"] == "planned"


def test_run_replanning_attempt_suggest_writes_artifacts_without_mutating_plan(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"])
    second = add_task(
        plan, title="Task B", estimated_files=["src/b.py"], dependencies=[str(first["id"])]
    )
    first["status"] = "done"
    save_plan(paths, plan)

    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Open integration issue.",
        paths_in_scope=["src/b.py"],
        related_tasks=[str(first["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:test",
    )
    trigger = build_replanning_trigger(
        paths=paths, integration_mode="warn", merged_task_ids=[str(first["id"])]
    )
    assert trigger is not None
    captured: dict[str, object] = {}

    def fake_planner_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured["section"] = kwargs.get("relevant_knowledge_section")
        return PlannerTurnResult(
            assistant_message="Adjust remaining task.",
            questions=[],
            plan_update={"tasks_update": [{"id": str(second["id"]), "title": "Adjusted Task B"}]},
        )

    result = run_replanning_attempt(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        requested_mode="suggest",
        batch_index=1,
        merged_task_ids=[str(first["id"])],
        integration_result=_integration_result(
            paths=paths, batch_index=1, passed=False, summary="failed"
        ),
        trigger=trigger,
        planner_runner=fake_planner_runner,
    )

    assert result.proposal_generated is True
    assert result.applied is False
    assert result.plan_changed is False
    assert result.schedule_recomputed is False
    assert load_plan(paths)["tasks"][1]["title"] == "Task B"
    assert result.summary_path.exists()
    assert result.selected_knowledge_manifest_path.exists()
    assert result.selected_knowledge_summary_path.exists()
    assert "## Relevant Knowledge" in str(captured["section"])
    payload = json.loads(result.validation_path.read_text(encoding="utf-8"))
    assert payload["valid"] is True
    assert payload["apply_attempted"] is False
    assert payload["plan_changed"] is False
    assert payload["schedule_recompute_required"] is False
    evidence_payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert evidence_payload["selected_knowledge_manifest_path"].endswith(
        "/selected_knowledge/manifest.json"
    )


def test_run_replanning_attempt_apply_updates_remaining_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"])
    second = add_task(
        plan, title="Task B", estimated_files=["src/b.py"], dependencies=[str(first["id"])]
    )
    first["status"] = "done"
    save_plan(paths, plan)

    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Open integration issue.",
        paths_in_scope=["src/b.py"],
        related_tasks=[str(first["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:test",
    )
    trigger = build_replanning_trigger(
        paths=paths, integration_mode="warn", merged_task_ids=[str(first["id"])]
    )
    assert trigger is not None
    result = run_replanning_attempt(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        requested_mode="apply",
        batch_index=1,
        merged_task_ids=[str(first["id"])],
        integration_result=_integration_result(
            paths=paths, batch_index=1, passed=False, summary="failed"
        ),
        trigger=trigger,
        planner_runner=lambda **_kwargs: PlannerTurnResult(
            assistant_message="Adjust remaining task.",
            questions=[],
            plan_update={"tasks_update": [{"id": str(second["id"]), "title": "Adjusted Task B"}]},
        ),
    )

    assert result.applied is True
    assert result.plan_changed is True
    assert result.schedule_recomputed is False
    assert load_plan(paths)["tasks"][1]["title"] == "Adjusted Task B"
    payload = json.loads(result.validation_path.read_text(encoding="utf-8"))
    assert payload["apply_attempted"] is True
    assert payload["plan_changed"] is True
    assert payload["schedule_recompute_required"] is True


def test_run_replanning_attempt_apply_fails_closed_on_bad_plan_acceptance(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"])
    second = add_task(
        plan, title="Task B", estimated_files=["src/b.py"], dependencies=[str(first["id"])]
    )
    first["status"] = "done"
    save_plan(paths, plan)

    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Open integration issue.",
        paths_in_scope=["src/b.py"],
        related_tasks=[str(first["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:test",
    )
    trigger = build_replanning_trigger(
        paths=paths, integration_mode="warn", merged_task_ids=[str(first["id"])]
    )
    assert trigger is not None

    with pytest.raises(PlannerFailedError) as exc_info:
        run_replanning_attempt(
            paths=paths,
            plan=plan,
            cfg=AppConfig(model="test-model"),
            api_key_override="k",
            requested_mode="apply",
            batch_index=1,
            merged_task_ids=[str(first["id"])],
            integration_result=_integration_result(
                paths=paths, batch_index=1, passed=False, summary="failed"
            ),
            trigger=trigger,
            planner_runner=lambda **_kwargs: PlannerTurnResult(
                assistant_message="Add a docs-only follow-up.",
                questions=[],
                plan_update={
                    "tasks_add": [
                        {
                            "title": "Fix task B behavior",
                            "description": "Update the implementation for the remaining behavior.",
                            "dependencies": [str(first["id"])],
                            "estimated_files": ["README.md"],
                            "write_scope": ["README.md"],
                        }
                    ]
                },
            ),
        )

    assert exc_info.value.failure_category == FailureCategory.PLANNER_FAILED
    assert "R3" in str(exc_info.value)
    assert "write_scope is README/docs only" in str(exc_info.value)
    final_plan = load_plan(paths)
    assert len(final_plan["tasks"]) == 2
    final_second = next(task for task in final_plan["tasks"] if task["id"] == str(second["id"]))
    assert final_second["write_scope"] == ["src/b.py"]
    validation_payload = json.loads(
        (paths.plan_replans_dir / "replan_001" / "validation.json").read_text(encoding="utf-8")
    )
    assert validation_payload["valid"] is False
    assert any("R3" in item for item in validation_payload["errors"])


def test_run_replanning_attempt_corrects_follow_up_scope_using_host_evidence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    report_task = add_task(
        plan,
        title="Render owner summary in the report",
        estimated_files=["taskboard/report.py", "tests/test_report.py"],
    )
    remaining = add_task(
        plan,
        title="Keep existing planned work",
        estimated_files=["taskboard/cli.py"],
        dependencies=[str(report_task["id"])],
    )
    report_task["status"] = "done"
    save_plan(paths, plan)

    write_task_attempt_entry(
        paths=paths,
        task=report_task,
        source="worker",
        result="success",
        summary="Updated owner summary rendering in the report output.",
        changed_files=["taskboard/report.py", "tests/test_report.py"],
        verify_summary="pytest -q passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="accepted",
        extra_tags=["execution", "worker"],
    )
    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: owner summary ordering is wrong",
        summary="The owner summary ordering is still wrong in the report output.",
        paths_in_scope=["taskboard/report.py", "tests/test_report.py"],
        related_tasks=[str(report_task["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:owner-summary",
    )
    trigger = build_replanning_trigger(
        paths=paths,
        integration_mode="warn",
        merged_task_ids=[str(report_task["id"])],
    )
    assert trigger is not None

    result = run_replanning_attempt(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        requested_mode="apply",
        batch_index=1,
        merged_task_ids=[str(report_task["id"])],
        integration_result=_integration_result(
            paths=paths, batch_index=1, passed=False, summary="failed"
        ),
        trigger=trigger,
        planner_runner=lambda **_kwargs: PlannerTurnResult(
            assistant_message="Add a follow-up for the owner summary ordering.",
            questions=[],
            plan_update={
                "tasks_add": [
                    {
                        "title": "Sort owner summary alphabetically with unassigned last",
                        "description": "Follow up on the remaining owner summary ordering issue.",
                        "dependencies": [str(report_task["id"])],
                        "estimated_files": ["taskboard/summary.py", "tests/test_summary.py"],
                        "write_scope": ["taskboard/summary.py", "tests/test_summary.py"],
                    }
                ]
            },
        ),
    )

    assert result.validation_passed is True
    final_plan = load_plan(paths)
    new_task = next(
        task
        for task in final_plan["tasks"]
        if task["id"] not in {str(report_task["id"]), str(remaining["id"])}
    )
    assert new_task["estimated_files"] == ["taskboard/report.py", "tests/test_report.py"]
    assert new_task["write_scope"] == ["taskboard/report.py", "tests/test_report.py"]
    plan_update_payload = json.loads(result.plan_update_path.read_text(encoding="utf-8"))
    assert plan_update_payload["plan_update"]["tasks_add"][0]["estimated_files"] == [
        "taskboard/report.py",
        "tests/test_report.py",
    ]
    validation_payload = json.loads(result.validation_path.read_text(encoding="utf-8"))
    assert any(
        "replanning corrected path metadata" in item and "taskboard/report.py" in item
        for item in validation_payload["warnings"]
    )


def test_run_replanning_attempt_prefers_explicit_follow_up_path_hints_for_new_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    report_task = add_task(
        plan,
        title="Render owner summary in the report",
        estimated_files=["taskboard/report.py", "tests/test_report.py"],
    )
    report_task["status"] = "done"
    save_plan(paths, plan)

    write_task_attempt_entry(
        paths=paths,
        task=report_task,
        source="worker",
        result="success",
        summary="Updated owner summary rendering in the report output.",
        changed_files=["taskboard/report.py", "tests/test_report.py"],
        verify_summary="pytest -q passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="accepted",
        extra_tags=["execution", "worker"],
    )
    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: owner summary docs follow-up",
        summary="Document the owner summary behavior after the report fix.",
        paths_in_scope=["taskboard/report.py", "tests/test_report.py"],
        related_tasks=[str(report_task["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:owner-summary-docs",
    )
    trigger = build_replanning_trigger(
        paths=paths,
        integration_mode="warn",
        merged_task_ids=[str(report_task["id"])],
    )
    assert trigger is not None

    result = run_replanning_attempt(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        requested_mode="suggest",
        batch_index=1,
        merged_task_ids=[str(report_task["id"])],
        integration_result=_integration_result(
            paths=paths, batch_index=1, passed=False, summary="failed"
        ),
        trigger=trigger,
        planner_runner=lambda **_kwargs: PlannerTurnResult(
            assistant_message="Add the docs follow-up.",
            questions=[],
            plan_update={
                "tasks_add": [
                    {
                        "title": "Document docs/owner-summary.md follow-up",
                        "description": "Add docs/owner-summary.md to explain the owner summary ordering behavior.",
                        "dependencies": [str(report_task["id"])],
                        "estimated_files": ["taskboard/summary.py"],
                        "write_scope": ["taskboard/summary.py"],
                    }
                ]
            },
        ),
    )

    assert result.validation_passed is True
    plan_update_payload = json.loads(result.plan_update_path.read_text(encoding="utf-8"))
    grounded_task = plan_update_payload["plan_update"]["tasks_add"][0]
    assert grounded_task["estimated_files"] == ["docs/owner-summary.md"]
    assert grounded_task["write_scope"] == ["docs/owner-summary.md"]
    validation_payload = json.loads(result.validation_path.read_text(encoding="utf-8"))
    assert validation_payload["valid"] is True
    assert not validation_payload["errors"]


def test_run_replanning_attempt_rejects_ungrounded_follow_up_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(plan, title="Task A", estimated_files=["src/a.py"])
    first["status"] = "done"
    save_plan(paths, plan)

    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: unrelated parser issue",
        summary="The parser still needs cleanup.",
        paths_in_scope=["src/parser.py"],
        related_tasks=[str(first["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:parser",
    )
    trigger = build_replanning_trigger(
        paths=paths,
        integration_mode="warn",
        merged_task_ids=[str(first["id"])],
    )
    assert trigger is not None

    result = run_replanning_attempt(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        requested_mode="suggest",
        batch_index=1,
        merged_task_ids=[str(first["id"])],
        integration_result=_integration_result(
            paths=paths, batch_index=1, passed=False, summary="failed"
        ),
        trigger=trigger,
        planner_runner=lambda **_kwargs: PlannerTurnResult(
            assistant_message="Add a follow-up.",
            questions=[],
            plan_update={
                "tasks_add": [
                    {
                        "title": "Sort owner summary alphabetically with unassigned last",
                        "description": "Follow up on the remaining owner summary ordering issue.",
                        "dependencies": [str(first["id"])],
                        "estimated_files": ["taskboard/summary.py", "tests/test_summary.py"],
                        "write_scope": ["taskboard/summary.py", "tests/test_summary.py"],
                    }
                ]
            },
        ),
    )

    assert result.validation_passed is False
    validation_payload = json.loads(result.validation_path.read_text(encoding="utf-8"))
    assert any("ungrounded path metadata" in item for item in validation_payload["errors"])


def test_run_replanning_attempt_keeps_existing_scope_for_task_updates(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    first = add_task(
        plan,
        title="Render owner summary in the report",
        estimated_files=["taskboard/report.py", "tests/test_report.py"],
    )
    second = add_task(
        plan,
        title="Adjust owner summary ordering",
        estimated_files=["taskboard/report.py", "tests/test_report.py"],
        dependencies=[str(first["id"])],
    )
    first["status"] = "done"
    second["write_scope"] = ["taskboard/report.py", "tests/test_report.py"]
    save_plan(paths, plan)

    write_task_attempt_entry(
        paths=paths,
        task=first,
        source="worker",
        result="success",
        summary="Updated owner summary rendering in the report output.",
        changed_files=["taskboard/report.py", "tests/test_report.py"],
        verify_summary="pytest -q passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="accepted",
        extra_tags=["execution", "worker"],
    )
    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: owner summary docs follow-up",
        summary="Document the owner summary behavior in docs/owner-summary.md.",
        paths_in_scope=["docs/owner-summary.md"],
        related_tasks=[str(first["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:owner-summary-docs",
    )
    trigger = build_replanning_trigger(
        paths=paths,
        integration_mode="warn",
        merged_task_ids=[str(first["id"])],
    )
    assert trigger is not None

    result = run_replanning_attempt(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        requested_mode="apply",
        batch_index=1,
        merged_task_ids=[str(first["id"])],
        integration_result=_integration_result(
            paths=paths, batch_index=1, passed=False, summary="failed"
        ),
        trigger=trigger,
        planner_runner=lambda **_kwargs: PlannerTurnResult(
            assistant_message="Update the existing owner summary task.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(second["id"]),
                        "title": "Adjust owner summary ordering and edge cases",
                        "estimated_files": ["docs/owner-summary.md"],
                        "write_scope": ["docs/owner-summary.md"],
                    }
                ]
            },
        ),
    )

    assert result.validation_passed is True
    final_plan = load_plan(paths)
    updated = next(task for task in final_plan["tasks"] if task["id"] == str(second["id"]))
    assert updated["estimated_files"] == ["taskboard/report.py", "tests/test_report.py"]
    assert updated["write_scope"] == ["taskboard/report.py", "tests/test_report.py"]
    validation_payload = json.loads(result.validation_path.read_text(encoding="utf-8"))
    assert any(
        "existing task scope" in item and "taskboard/report.py" in item
        for item in validation_payload["warnings"]
    )


def test_run_replanning_attempt_prefers_full_accepted_attempt_scope_over_partial_issue_scope(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    report_task = add_task(
        plan,
        title="Render owner summary in the report",
        estimated_files=["taskboard/report.py", "tests/test_report.py"],
    )
    report_task["status"] = "done"
    save_plan(paths, plan)

    write_task_attempt_entry(
        paths=paths,
        task=report_task,
        source="worker",
        result="success",
        summary="Updated owner summary rendering in the report output.",
        changed_files=["taskboard/report.py", "tests/test_report.py"],
        verify_summary="pytest -q passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="accepted",
        extra_tags=["execution", "worker"],
    )
    write_issue_entry_for_task_id(
        paths=paths,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: owner summary ordering is wrong",
        summary="The owner summary ordering is still wrong in the report output.",
        paths_in_scope=["taskboard/report.py"],
        related_tasks=[str(report_task["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature="integration_gate_v1:owner-summary-partial",
    )
    trigger = build_replanning_trigger(
        paths=paths,
        integration_mode="warn",
        merged_task_ids=[str(report_task["id"])],
    )
    assert trigger is not None

    result = run_replanning_attempt(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        requested_mode="suggest",
        batch_index=1,
        merged_task_ids=[str(report_task["id"])],
        integration_result=_integration_result(
            paths=paths, batch_index=1, passed=False, summary="failed"
        ),
        trigger=trigger,
        planner_runner=lambda **_kwargs: PlannerTurnResult(
            assistant_message="Add a follow-up for the owner summary ordering.",
            questions=[],
            plan_update={
                "tasks_add": [
                    {
                        "title": "Sort owner summary alphabetically with unassigned last",
                        "description": "Follow up on the remaining owner summary ordering issue.",
                        "dependencies": [str(report_task["id"])],
                        "estimated_files": ["taskboard/summary.py", "tests/test_summary.py"],
                        "write_scope": ["taskboard/summary.py", "tests/test_summary.py"],
                    }
                ]
            },
        ),
    )

    assert result.validation_passed is True
    plan_update_payload = json.loads(result.plan_update_path.read_text(encoding="utf-8"))
    grounded_task = plan_update_payload["plan_update"]["tasks_add"][0]
    assert grounded_task["estimated_files"] == ["taskboard/report.py", "tests/test_report.py"]
    assert grounded_task["write_scope"] == ["taskboard/report.py", "tests/test_report.py"]
