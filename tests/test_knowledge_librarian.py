from __future__ import annotations

import json
from pathlib import Path

from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.integration_gate import integration_issue_signature_for_commands
from alysis_code.knowledge_base import (
    rebuild_knowledge_index,
    write_decision_entry,
    write_fact_entry,
    write_issue_entry,
    write_issue_entry_for_task_id,
    write_task_attempt_entry,
    write_task_attempt_resolution_entry,
)
from alysis_code.knowledge_capture import persist_execution_knowledge_capture
from alysis_code.knowledge_librarian import (
    prepare_planner_knowledge,
    prepare_relevant_knowledge,
    select_relevant_knowledge,
)


def test_librarian_prefers_open_issue_with_matching_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_task_attempt_entry(
        paths=first_run,
        task=prior_task,
        source="forge_exec",
        result="success",
        summary="Parser retry implementation landed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        created_at="2026-03-11T12:00:00Z",
    )
    write_issue_entry(
        paths=first_run,
        task=prior_task,
        source="forge_exec",
        title="Parser retry still flakes under load",
        summary="Strict verification still fails in src/parser.py.",
        paths_in_scope=["src/parser.py"],
        tags=["verification_failure"],
        created_at="2026-03-12T09:00:00Z",
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Refine parser retry behavior",
        description="Use prior parser retry work and address remaining flaky cases.",
        estimated_files=["src/parser.py"],
        dependencies=[str(prior_task["id"])],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(paths=second_run, task=current_task)

    assert selections
    assert selections[0].entry.kind == "issue"
    assert "path overlap: src/parser.py" in selections[0].reasons
    assert "open issue boost" in selections[0].reasons


def test_prepare_relevant_knowledge_materializes_manifest_and_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_task_attempt_entry(
        paths=first_run,
        task=prior_task,
        source="swarm_worker",
        result="success",
        summary="Worker updated src/parser.py.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Follow up parser retry task",
        estimated_files=["src/parser.py"],
    )
    save_plan(second_run, plan)

    prepared = prepare_relevant_knowledge(
        paths=second_run,
        task=current_task,
        selection_label="execution",
    )

    assert prepared.manifest_path.exists()
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    assert manifest["task_id"] == current_task["id"]
    assert manifest["selection_label"] == "execution"
    assert manifest["selected_entries"]
    copied_entry_path = second_run.root / manifest["selected_entries"][0]["materialized_path"]
    assert copied_entry_path.exists()
    section = prepared.render_prompt_section(workspace_root=second_run.root)
    assert "## Relevant Knowledge" in section
    assert "Selected Knowledge Files" in section


def test_librarian_selection_survives_invalid_knowledge_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_task_attempt_entry(
        paths=first_run,
        task=prior_task,
        source="forge_exec",
        result="success",
        summary="Parser retry implementation landed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    invalid_path = first_run.knowledge_issues_dir / str(prior_task["id"]) / "broken_issue.md"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_text('---\nkind: "issue"\nnot-valid\n', encoding="utf-8")
    index = rebuild_knowledge_index(first_run)
    assert len(index.invalid_entries) == 1

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Refine parser retry behavior",
        estimated_files=["src/parser.py"],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(paths=second_run, task=current_task)

    assert selections
    assert selections[0].entry.kind == "task_attempt"
    prepared = prepare_relevant_knowledge(
        paths=second_run,
        task=current_task,
        selection_label="execution",
    )
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    assert manifest["selected_entries"]


def test_librarian_selects_open_integration_issue_for_follow_up_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_issue_entry_for_task_id(
        paths=first_run,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Merged parser changes broke the repo-level verification gate.",
        paths_in_scope=["src/parser.py"],
        related_tasks=[str(prior_task["id"])],
        tags=["integration_gate", "integration_failure"],
        created_at="2026-03-12T09:00:00Z",
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Stabilize parser integration behavior",
        description="Follow up on integration failures after parser retry landed.",
        estimated_files=["src/parser.py"],
        dependencies=[str(prior_task["id"])],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(paths=second_run, task=current_task)

    assert selections
    assert selections[0].entry.source == "integration_gate"
    assert "open issue boost" in selections[0].reasons


def test_librarian_does_not_boost_resolved_integration_issue(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    signature = integration_issue_signature_for_commands(("pytest -q",))
    failed_issue = write_issue_entry_for_task_id(
        paths=first_run,
        task_id="batch_001",
        source="integration_gate",
        title="batch_001: integration verification failed",
        summary="Merged parser changes broke the repo-level verification gate.",
        paths_in_scope=["src/parser.py"],
        related_tasks=[str(prior_task["id"])],
        tags=["integration_gate", "integration_failure"],
        status="open",
        signature=signature,
    )

    second_run = create_plan_run(repo)
    write_issue_entry_for_task_id(
        paths=second_run,
        task_id="batch_002",
        source="integration_gate",
        title="batch_002: integration verification passed",
        summary="Later batch passed the same integration gate.",
        paths_in_scope=["src/parser.py"],
        related_tasks=[str(prior_task["id"])],
        tags=["integration_gate", "integration_resolution"],
        status="resolved",
        signature=signature,
        resolves=[failed_issue.id],
    )
    rebuild_knowledge_index(second_run)

    third_run = create_plan_run(repo)
    plan = load_plan(third_run)
    current_task = add_task(
        plan,
        title="Stabilize parser integration behavior",
        description="Follow up after parser integration recovered.",
        estimated_files=["src/parser.py"],
        dependencies=[str(prior_task["id"])],
    )
    save_plan(third_run, plan)

    selections = select_relevant_knowledge(paths=third_run, task=current_task)

    assert selections
    integration_selections = [
        item for item in selections if item.entry.source == "integration_gate"
    ]
    assert integration_selections
    for selection in integration_selections:
        assert "open issue boost" not in selection.reasons


def test_librarian_prefers_active_decision_over_invalidated_decision(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(
        plan,
        title="Parser retry decisions",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_decision_entry(
        paths=first_run,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Keep bounded parser retry backoff",
        summary="Decision remains active for parser retries.",
        status="active",
        paths_in_scope=["src/parser.py"],
        tags=["parser", "retry"],
        created_at="2026-03-12T13:00:00Z",
    )
    write_decision_entry(
        paths=first_run,
        task=task,
        source="forge_exec",
        decision_key="parser-batch-window",
        title="Retire parser batch window retry heuristic",
        summary="Decision is now invalidated.",
        status="invalidated",
        paths_in_scope=["src/parser.py"],
        tags=["parser", "retry"],
        created_at="2026-03-12T13:00:00Z",
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Refine parser retry behavior",
        description="Follow active parser retry guidance.",
        estimated_files=["src/parser.py"],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(paths=second_run, task=current_task)

    assert selections
    decision_selections = [item for item in selections if item.entry.kind == "decision"]
    assert decision_selections
    assert decision_selections[0].entry.effective_status == "active"
    assert "active decision boost" in decision_selections[0].reasons


def test_librarian_includes_facts_in_retrieval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(
        plan,
        title="Parser fact capture",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_fact_entry(
        paths=first_run,
        task=task,
        source="swarm_worker",
        title="Parser retries cap at three attempts",
        summary="Observed parser retry loop caps attempts at three.",
        paths_in_scope=["src/parser.py"],
        tags=["parser", "retry"],
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Refine parser retry behavior",
        estimated_files=["src/parser.py"],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(paths=second_run, task=current_task)

    fact_selection = next(selection for selection in selections if selection.entry.kind == "fact")
    assert "recorded fact context" in fact_selection.reasons


def test_librarian_does_not_select_unrelated_recent_fact_without_anchor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(
        plan,
        title="Capture CLI fact",
        estimated_files=["src/cli.py"],
    )
    save_plan(first_run, plan)
    write_fact_entry(
        paths=first_run,
        task=task,
        source="forge_exec",
        title="CLI parser defaults to legacy flags",
        summary="Observed legacy flag defaults in src/cli.py.",
        paths_in_scope=["src/cli.py"],
        tags=["cli", "flags"],
        created_at="2026-03-12T13:00:00Z",
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Refine database transaction retries",
        estimated_files=["src/db.py"],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(
        paths=second_run,
        task=current_task,
        consumer="planner",
    )

    assert selections == ()


def test_librarian_does_not_select_unrelated_active_decision_without_anchor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(
        plan,
        title="Parser retry decision",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_decision_entry(
        paths=first_run,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Keep parser retry backoff",
        summary="Active decision for src/parser.py.",
        status="active",
        paths_in_scope=["src/parser.py"],
        tags=["parser", "retry"],
        created_at="2026-03-12T13:00:00Z",
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Refine unrelated email templates",
        estimated_files=["src/email.py"],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(
        paths=second_run,
        task=current_task,
        consumer="planner",
    )

    assert selections == ()


def test_librarian_does_not_match_cross_run_task_id_as_same_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    prior_task = add_task(
        plan,
        title="Parser history task",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_task_attempt_entry(
        paths=first_run,
        task=prior_task,
        source="forge_exec",
        result="success",
        summary="Parser task succeeded.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Database migration task",
        estimated_files=["src/db.py"],
    )
    save_plan(second_run, plan)

    assert prior_task["id"] == current_task["id"]
    selections = select_relevant_knowledge(paths=second_run, task=current_task)

    assert selections == ()


def test_planner_does_not_select_cross_run_issue_from_related_task_id_alias(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    issue_task = add_task(
        plan,
        title="Legacy parser issue task",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_issue_entry_for_task_id(
        paths=first_run,
        task_id="batch_001",
        source="integration_gate",
        title="Unrelated parser integration issue",
        summary="Open issue from another run.",
        paths_in_scope=["src/parser.py"],
        related_tasks=[str(issue_task["id"])],
        tags=["integration_gate", "integration_failure"],
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    add_task(
        plan,
        title="Current database task",
        estimated_files=["src/db.py"],
    )
    save_plan(second_run, plan)

    prepared = prepare_planner_knowledge(
        paths=second_run,
        plan=plan,
        user_text="Please refine T01",
    )

    assert prepared.selections == ()


def test_planner_does_not_select_cross_run_fact_or_decision_from_related_task_id_alias(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    source_task = add_task(
        plan,
        title="Legacy parser semantic task",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_fact_entry(
        paths=first_run,
        task=source_task,
        source="forge_exec",
        title="Legacy parser fact",
        summary="Recorded fact from another run.",
        paths_in_scope=["src/parser.py"],
        related_tasks=[str(source_task["id"])],
        tags=["parser"],
    )
    write_decision_entry(
        paths=first_run,
        task=source_task,
        source="forge_exec",
        decision_key="legacy-parser-decision",
        title="Legacy parser decision",
        summary="Active decision from another run.",
        status="active",
        paths_in_scope=["src/parser.py"],
        related_tasks=[str(source_task["id"])],
        tags=["parser"],
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    add_task(
        plan,
        title="Current database task",
        estimated_files=["src/db.py"],
    )
    save_plan(second_run, plan)

    prepared = prepare_planner_knowledge(
        paths=second_run,
        plan=plan,
        user_text="Please refine T01",
    )

    assert prepared.selections == ()


def test_librarian_same_run_dependency_link_is_run_aware(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    dependency_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    current_task = add_task(
        plan,
        title="Stabilize parser follow-up",
        description="Follow the earlier parser retry implementation.",
        estimated_files=["src/parser.py"],
        dependencies=[str(dependency_task["id"])],
    )
    save_plan(paths, plan)
    write_task_attempt_entry(
        paths=paths,
        task=dependency_task,
        source="forge_exec",
        result="success",
        summary="Parser retry implementation landed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    rebuild_knowledge_index(paths)

    selections = select_relevant_knowledge(paths=paths, task=current_task)

    assert selections
    assert selections[0].entry.run_id == paths.run_id
    assert any(reason.startswith("dependency link:") for reason in selections[0].reasons)


def test_librarian_only_boosts_effectively_accepted_task_attempts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    current_task = add_task(
        plan,
        title="Refine parser retry behavior",
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)

    pending_attempt = write_task_attempt_entry(
        paths=paths,
        task=prior_task,
        source="swarm_worker",
        result="success",
        summary="Worker completed successfully.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="pending",
    )
    rebuild_knowledge_index(paths)

    pending_selections = select_relevant_knowledge(
        paths=paths, task=current_task, consumer="planner"
    )

    assert pending_selections
    assert pending_selections[0].entry.id == pending_attempt.id
    assert "accepted task attempt context" not in pending_selections[0].reasons

    write_task_attempt_resolution_entry(
        paths=paths,
        task=prior_task,
        source="swarm_orchestrator",
        acceptance_state="accepted",
        resolved_attempt_id=pending_attempt.id,
        summary="Worker result was accepted after merge.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    rebuild_knowledge_index(paths)

    accepted_selections = select_relevant_knowledge(
        paths=paths, task=current_task, consumer="planner"
    )

    assert accepted_selections
    assert accepted_selections[0].entry.id == pending_attempt.id
    assert "accepted task attempt context" in accepted_selections[0].reasons


def test_librarian_does_not_restore_stale_task_attempt_boost_from_incomplete_cache(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    current_task = add_task(
        plan,
        title="Refine parser retry behavior",
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)

    pending_attempt = write_task_attempt_entry(
        paths=paths,
        task=prior_task,
        source="swarm_worker",
        result="success",
        summary="Worker completed successfully.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        acceptance_state="pending",
    )
    write_task_attempt_resolution_entry(
        paths=paths,
        task=prior_task,
        source="swarm_orchestrator",
        acceptance_state="rejected",
        resolved_attempt_id=pending_attempt.id,
        summary="Worker result was rejected after merge/apply failed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest failed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    rebuild_knowledge_index(paths)
    payload = json.loads(paths.knowledge_index_path.read_text(encoding="utf-8"))
    for entry in payload.get("entries") or []:
        if isinstance(entry, dict):
            entry.pop("effective_status", None)
    paths.knowledge_index_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    selections = select_relevant_knowledge(paths=paths, task=current_task, consumer="planner")

    assert selections
    selected_attempt = next(item for item in selections if item.entry.id == pending_attempt.id)
    assert "accepted task attempt context" not in selected_attempt.reasons


def test_librarian_ignores_unpromoted_structured_capture_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(
        plan,
        title="Candidate parser retry change",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    persist_execution_knowledge_capture(
        paths=first_run,
        task=task,
        source="swarm_worker",
        assistant_message="\n".join(
            [
                "Candidate worker summary.",
                "",
                "```knowledge_capture_json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "facts": [
                            {
                                "title": "Candidate parser retry fact",
                                "summary": "Observed bounded parser retry behavior.",
                                "paths": ["src/parser.py"],
                            }
                        ],
                        "decisions": [
                            {
                                "decision_key": "candidate-parser-retry",
                                "title": "Candidate parser retry decision",
                                "summary": "Keep the candidate parser retry behavior.",
                                "status": "active",
                                "paths": ["src/parser.py"],
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                "```",
            ]
        ),
        artifact_dir=first_run.execution_knowledge_capture_dir / str(task["id"]) / "attempt_001",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Follow up parser retry behavior",
        estimated_files=["src/parser.py"],
    )
    save_plan(second_run, plan)

    selections = select_relevant_knowledge(paths=second_run, task=current_task)

    assert all(selection.entry.kind not in {"fact", "decision"} for selection in selections)


def test_prepare_planner_knowledge_materializes_under_plan_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_decision_entry(
        paths=first_run,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Keep bounded parser retry backoff",
        summary="The parser retry backoff should stay bounded.",
        status="active",
        paths_in_scope=["src/parser.py"],
        tags=["parser", "retry"],
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    add_task(
        plan,
        title="Follow up parser work",
        estimated_files=["src/parser.py"],
    )
    save_plan(second_run, plan)

    prepared = prepare_planner_knowledge(
        paths=second_run,
        plan=plan,
        user_text="Update the parser retry plan around src/parser.py",
    )

    assert prepared.manifest_path.exists()
    assert prepared.summary_path.exists()
    assert prepared.selected_dir == second_run.plan_dir / "selected_knowledge" / "planner"
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    assert manifest["selection_label"] == "planner"
    assert manifest["selected_entries"]


def test_librarian_planner_and_replanner_profiles_weight_decisions_and_open_issues(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    first_run = create_plan_run(repo)
    plan = load_plan(first_run)
    task = add_task(
        plan,
        title="Parser retry work",
        estimated_files=["src/parser.py"],
    )
    save_plan(first_run, plan)
    write_fact_entry(
        paths=first_run,
        task=task,
        source="forge_exec",
        title="Parser retry uses bounded backoff",
        summary="Observed bounded retry backoff in src/parser.py.",
        paths_in_scope=["src/parser.py"],
        tags=["parser", "retry"],
    )
    write_decision_entry(
        paths=first_run,
        task=task,
        source="forge_exec",
        decision_key="parser-retry-backoff",
        title="Keep bounded parser retry backoff",
        summary="Active retry backoff decision for src/parser.py.",
        status="active",
        paths_in_scope=["src/parser.py"],
        tags=["parser", "retry"],
    )
    write_issue_entry(
        paths=first_run,
        task=task,
        source="integration_gate",
        title="Parser integration issue remains open",
        summary="Open integration issue in src/parser.py.",
        paths_in_scope=["src/parser.py"],
        tags=["integration_gate", "integration_failure"],
    )
    rebuild_knowledge_index(first_run)

    second_run = create_plan_run(repo)
    plan = load_plan(second_run)
    current_task = add_task(
        plan,
        title="Refine parser retry behavior",
        estimated_files=["src/parser.py"],
    )
    save_plan(second_run, plan)

    planner_selections = select_relevant_knowledge(
        paths=second_run,
        task=current_task,
        consumer="planner",
    )
    replanner_selections = select_relevant_knowledge(
        paths=second_run,
        task=current_task,
        consumer="replanner",
    )

    assert planner_selections
    assert replanner_selections
    assert planner_selections[0].entry.kind == "decision"
    assert "active decision boost" in planner_selections[0].reasons
    assert replanner_selections[0].entry.kind == "issue"
    assert "open issue boost" in replanner_selections[0].reasons
