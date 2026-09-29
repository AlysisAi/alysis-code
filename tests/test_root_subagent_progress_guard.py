from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from alysis_code.agent.turn.subagent_progress import (
    RootSubagentLaunchGuard,
    RootSubagentProgressGuard,
    subagent_launch_intent_fingerprint,
    subagent_semantic_outcome_fingerprint,
    terminal_captured_duplicate_subagent_run,
    tool_result_advances_root_objective_state,
)
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.subagents import SubagentDefinition


def _isolated_result(
    *,
    run_number: int,
    patch_sha256: str = "a" * 64,
    base_commit: str = "base-a",
    semantic_no_progress: bool | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": f"run-{run_number}",
        "subagent": "implementer",
        "subagent_session_id": f"child-session-{run_number}",
        "status": "success",
        "result": f"Child prose variant {run_number}",
        "result_source": "session_final_event",
        "elapsed_ms": run_number * 101,
        "steps_completed": run_number,
        "usage": {"total_tokens": run_number * 1000},
        "effects": ["write_workspace", "delegate"],
        "touched_repo_paths": ["candidate.txt"],
        "material_touched_repo_paths": ["candidate.txt"],
        "workspace": {
            "view": "isolated",
            "base_commit": base_commit,
            "state": "captured",
            "parent_dirty_paths": [],
            "no_changes": False,
        },
        "patch_summary": {
            "files": ["candidate.txt"],
            "insertions": 1,
            "deletions": 1,
            "sha256": patch_sha256,
            "patch_artifact": f"session-artifact://subagent_patches/run-{run_number}.patch",
        },
    }
    if semantic_no_progress is not None:
        result["semantic_no_progress"] = semantic_no_progress
    return result


def _already_integrated_apply_result(run_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "run_id": run_id,
        "action": "already_integrated",
        "already_integrated": True,
        "semantic_no_progress": True,
        "cleanup_pending": False,
        "physical_worktree_removed": True,
        "applied_paths": [],
        "integrated_paths": ["candidate.txt"],
    }


def _captured_duplicate_result(
    *,
    run_number: int = 2,
    canonical_run_id: str = "canonical-run",
) -> dict[str, Any]:
    result = _isolated_result(run_number=run_number, semantic_no_progress=True)
    result.update(
        {
            "duplicate_of": canonical_run_id,
            "canonical_run_id": canonical_run_id,
            "canonical_state": "captured",
            "already_integrated": False,
            "candidate_worktree_retained": False,
            "cleanup_pending": False,
            "physical_worktree_removed": True,
            "material_identity_sha256": "b" * 64,
        }
    )
    return result


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _create_subagent_test_session(tmp_path: Path, *, semantic_guard: bool) -> Any:
    orchestration = (
        {
            "repetition_signal_threshold": 2,
            "repetition_nudge_occurrence_threshold": 2,
            "repetition_occurrence_threshold": 3,
            "repetition_backstop_threshold": 4,
        }
        if semantic_guard
        else {}
    )
    return create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="auto",
            step_budget_policy="adaptive" if semantic_guard else "fixed",
            subagent_orchestration=orchestration,
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_chat_turn_step_budget=semantic_guard,
        verification_enabled=False,
        subagents_enabled=True,
        subagent_registry={
            "implementer": SubagentDefinition(
                name="implementer",
                description="Implement a scoped candidate.",
                system_prompt="Implement the requested candidate.",
                mode="auto",
            )
        },
    )


def test_launch_identity_excludes_transport_labels_and_operational_limits() -> None:
    canonical = {
        "name": "explorer",
        "task": "Read service_contract.md and report its invariants.\n",
        "mode": "readonly",
        "workspace_view": "shared",
        "workspace_from_run": "source-run",
        "depends_on": ["dependency-b", "dependency-a"],
        "run_id": "child-a",
        "label": "first display label",
        "max_steps": 1,
        "timeout": 2,
        "deadline": "soon",
    }
    transport_variant = {
        **canonical,
        "run_id": "phase-five-child-a",
        "label": "different display label",
        "depends_on": ["dependency-a", "dependency-b"],
        "max_steps": 999,
        "timeout": 999,
        "deadline": "later",
    }

    assert subagent_launch_intent_fingerprint(transport_variant) == (
        subagent_launch_intent_fingerprint(canonical)
    )


def test_launch_identity_ignores_presentation_only_task_reflow() -> None:
    canonical = {
        "name": "explorer",
        "task": "Read service_contract.md\nand report its invariants.",
        "mode": "readonly",
        "workspace_view": "shared",
    }
    reflowed = {
        **canonical,
        "task": "  Read   service_contract.md  and report its invariants.  ",
    }

    assert subagent_launch_intent_fingerprint(reflowed) == (
        subagent_launch_intent_fingerprint(canonical)
    )


def test_launch_identity_preserves_each_substantive_typed_input() -> None:
    canonical = {
        "name": "explorer",
        "task": "Read service_contract.md and report its invariants.\n",
        "mode": "readonly",
        "workspace_view": "shared",
        "workspace_from_run": "source-run",
        "depends_on": ["dependency-a"],
    }
    variants = (
        {**canonical, "name": "verifier"},
        {**canonical, "task": "Read incident_policy.md and report its invariants."},
        {**canonical, "mode": "review"},
        {**canonical, "workspace_view": "isolated"},
        {**canonical, "workspace_from_run": "other-source-run"},
        {**canonical, "depends_on": ["dependency-b"]},
    )
    canonical_fingerprint = subagent_launch_intent_fingerprint(canonical)

    assert all(
        subagent_launch_intent_fingerprint(variant) != canonical_fingerprint for variant in variants
    )


def test_foreground_replay_latch_is_structural_and_revision_scoped() -> None:
    guard = RootSubagentProgressGuard(recent_window=8)
    canonical = {
        "name": "implementer",
        "task": "Open candidate.env, then edit only candidate.env.",
        "mode": "auto",
        "workspace_view": "isolated",
    }
    reflowed = {
        **canonical,
        "task": "Open candidate.env,\nthen edit only candidate.env.",
    }

    first_result = _isolated_result(run_number=1, semantic_no_progress=False)
    first = guard.observe(
        arguments=canonical,
        result=first_result,
        tool_status="done",
    )
    assert first is not None and first.semantic_no_progress is False
    guard.record_foreground_result(
        arguments=canonical,
        result=first_result,
        tool_status="done",
        semantic_no_progress=False,
    )

    coalesced = guard.inspect_foreground_launch(reflowed)
    assert coalesced.allow_dispatch is False
    assert coalesced.stop_signal is False
    coalesced_result = coalesced.coalesced_result()
    assert coalesced_result["run_id"] == "run-1"
    assert coalesced_result["canonical_run_id"] == "run-1"
    assert coalesced_result["semantic_no_progress"] is True

    duplicate = guard.observe(
        arguments=reflowed,
        result=coalesced_result,
        tool_status="done",
    )
    assert duplicate is not None and duplicate.semantic_no_progress is True
    guard.record_foreground_result(
        arguments=reflowed,
        result=coalesced_result,
        tool_status="done",
        semantic_no_progress=True,
    )

    blocked = guard.inspect_foreground_launch(canonical)
    assert blocked.allow_dispatch is False
    assert blocked.stop_signal is True
    assert blocked.stopped_result()["error_code"] == ("equivalent_foreground_subagent_replay")
    assert (
        guard.inspect_foreground_launch(
            {**canonical, "task": "Edit a distinct candidate path."}
        ).allow_dispatch
        is True
    )

    guard.note_objective_transition()
    assert guard.inspect_foreground_launch(canonical).allow_dispatch is True


def test_foreground_replay_latch_does_not_block_a_failed_retry() -> None:
    guard = RootSubagentProgressGuard(recent_window=8)
    arguments = {
        "name": "implementer",
        "task": "Edit candidate.env.",
        "mode": "auto",
        "workspace_view": "isolated",
    }

    assert (
        guard.observe(
            arguments=arguments,
            result={"status": "failed", "error": "transient provider failure"},
            tool_status="failed",
        )
        is None
    )
    guard.record_foreground_result(
        arguments=arguments,
        result={"status": "failed", "error": "transient provider failure"},
        tool_status="failed",
        semantic_no_progress=False,
    )
    assert guard.inspect_foreground_launch(arguments).allow_dispatch is True


def test_terminal_captured_duplicate_requires_complete_typed_transition() -> None:
    duplicate = _captured_duplicate_result()

    assert terminal_captured_duplicate_subagent_run(
        tool_name="subagent_run",
        result=duplicate,
        tool_status="done",
    )

    nonterminal_variants = (
        {**duplicate, "already_integrated": True, "canonical_state": "applied"},
        {**duplicate, "cleanup_pending": True, "physical_worktree_removed": False},
        {**duplicate, "candidate_worktree_retained": True},
        {**duplicate, "semantic_no_progress": False},
        {**duplicate, "duplicate_of": "different-canonical"},
        {**duplicate, "run_id": duplicate["canonical_run_id"]},
        {**duplicate, "ok": False},
        {**duplicate, "error": "capture failed", "status": "failed"},
    )
    assert all(
        not terminal_captured_duplicate_subagent_run(
            tool_name="subagent_run",
            result=variant,
            tool_status="done",
        )
        for variant in nonterminal_variants
    )
    assert not terminal_captured_duplicate_subagent_run(
        tool_name="subagent_wait",
        result=duplicate,
        tool_status="done",
    )


def test_lifecycle_replay_latch_is_exact_typed_and_revision_scoped() -> None:
    guard = RootSubagentProgressGuard(recent_window=8)
    arguments = {"run_id": "duplicate-run", "acknowledge_incomplete": False}

    available = guard.inspect_lifecycle_operation(
        tool_name="subagent_apply",
        arguments=arguments,
    )
    assert available is not None and available.allow_dispatch is True

    canonical_result = _already_integrated_apply_result("duplicate-run")
    guard.record_lifecycle_result(
        tool_name="subagent_apply",
        arguments=arguments,
        result=canonical_result,
        tool_status="done",
    )

    blocked = guard.inspect_lifecycle_operation(
        tool_name="SUBAGENT_APPLY",
        arguments=arguments,
    )
    assert blocked is not None and blocked.allow_dispatch is False
    assert blocked.stop_signal is True
    stopped_result = blocked.stopped_result()
    assert stopped_result["run_id"] == "duplicate-run"
    assert stopped_result["coalesced"] is True
    assert stopped_result["dispatch_blocked"] is True
    assert stopped_result["error_code"] == "equivalent_subagent_lifecycle_replay"

    changed_arguments = (
        {"run_id": "other-run", "acknowledge_incomplete": False},
        {"run_id": "duplicate-run", "acknowledge_incomplete": True},
    )
    for variant in changed_arguments:
        changed = guard.inspect_lifecycle_operation(
            tool_name="subagent_apply",
            arguments=variant,
        )
        assert changed is not None and changed.allow_dispatch is True

    # Discard is lifecycle-mutating even for an alias. It must reach the
    # provider so a later replay receives the authoritative released-state error.
    guard.record_lifecycle_result(
        tool_name="subagent_discard",
        arguments={"run_id": "duplicate-run"},
        result={
            "ok": True,
            "action": "discarded",
            "duplicate_of": "canonical-run",
            "semantic_no_progress": True,
        },
        tool_status="done",
    )
    discard = guard.inspect_lifecycle_operation(
        tool_name="subagent_discard",
        arguments={"run_id": "duplicate-run"},
    )
    assert discard is None
    after_discard = guard.inspect_lifecycle_operation(
        tool_name="subagent_apply",
        arguments=arguments,
    )
    assert after_discard is not None and after_discard.allow_dispatch is True

    guard.record_lifecycle_result(
        tool_name="subagent_apply",
        arguments=arguments,
        result=canonical_result,
        tool_status="done",
    )
    guard.note_objective_transition()
    after_transition = guard.inspect_lifecycle_operation(
        tool_name="subagent_apply",
        arguments=arguments,
    )
    assert after_transition is not None and after_transition.allow_dispatch is True


def test_lifecycle_replay_latch_preserves_failed_and_incomplete_retries() -> None:
    retryable_results = (
        ({"ok": False, "error": "transient provider failure"}, "failed"),
        (
            {
                **_already_integrated_apply_result("duplicate-run"),
                "cleanup_pending": True,
                "physical_worktree_removed": False,
            },
            "done",
        ),
        (
            {
                "ok": True,
                "action": "applied",
                "semantic_no_progress": False,
                "applied_paths": ["candidate.txt"],
            },
            "done",
        ),
    )

    for result, tool_status in retryable_results:
        guard = RootSubagentProgressGuard(recent_window=8)
        arguments = {"run_id": "duplicate-run"}
        guard.record_lifecycle_result(
            tool_name="subagent_apply",
            arguments=arguments,
            result=result,
            tool_status=tool_status,
        )
        decision = guard.inspect_lifecycle_operation(
            tool_name="subagent_apply",
            arguments=arguments,
        )
        assert decision is not None and decision.allow_dispatch is True


def test_launch_guard_allows_an_independently_named_replica_in_the_same_generation() -> None:
    guard = RootSubagentLaunchGuard()
    canonical = {
        "name": "explorer",
        "task": "Inspect the contract.",
        "mode": "readonly",
        "workspace_view": "shared",
        "run_id": "child-a",
    }
    guard.record_spawn_result(
        arguments=canonical,
        result={"run_id": "child-a", "state": "running"},
        tool_status="done",
        generation=1,
    )

    decision = guard.inspect_spawn(
        {**canonical, "run_id": "renamed-child"},
        generation=1,
    )

    assert decision.allow_dispatch is True
    assert decision.coalesced is False
    assert decision.semantic_no_progress is False
    assert decision.stop_signal is False
    assert decision.canonical_run_id == "child-a"
    assert decision.reason == "same_generation_replica"


def test_launch_guard_detects_rotation_after_structured_duplicate_id_failure() -> None:
    guard = RootSubagentLaunchGuard()
    canonical = {
        "name": "explorer",
        "task": "Inspect the contract.",
        "mode": "readonly",
        "workspace_view": "shared",
        "run_id": "child-a",
    }
    guard.record_spawn_result(
        arguments=canonical,
        result={"run_id": "child-a", "state": "finished"},
        tool_status="done",
        generation=1,
    )
    guard.record_spawn_result(
        arguments=canonical,
        result={
            "error": "scheduler conflict",
            "error_code": "duplicate_background_subagent_run_id",
            "run_id": "child-a",
        },
        tool_status="failed",
        generation=2,
    )

    decision = guard.inspect_spawn(
        {**canonical, "run_id": "rotated-child-a"},
        generation=3,
    )

    assert decision.allow_dispatch is False
    assert decision.coalesced is True
    assert decision.canonical_run_id == "child-a"
    assert decision.stop_signal is True
    assert decision.reason == "launch_intent_repeated_after_generation"


def test_launch_guard_detects_rotation_after_host_coalesced_duplicate() -> None:
    guard = RootSubagentLaunchGuard()
    canonical = {
        "name": "explorer",
        "task": "Inspect the contract.",
        "mode": "readonly",
        "workspace_view": "shared",
        "run_id": "child-a",
    }
    guard.record_spawn_result(
        arguments=canonical,
        result={"run_id": "child-a", "state": "finished"},
        tool_status="done",
        generation=1,
    )

    duplicate = guard.inspect_spawn(canonical, generation=1)
    rotated = guard.inspect_spawn(
        {**canonical, "run_id": "rotated-child-a"},
        generation=2,
    )

    assert duplicate.allow_dispatch is False
    assert duplicate.stop_signal is False
    assert duplicate.coalesced_result() == {
        "ok": True,
        "run_id": "child-a",
        "duplicate_of": "child-a",
        "coalesced": True,
        "semantic_no_progress": True,
        "launch_guard": duplicate.telemetry_payload(),
    }
    assert rotated.allow_dispatch is False
    assert rotated.coalesced is True
    assert rotated.canonical_run_id == "child-a"
    assert rotated.stop_signal is True
    assert rotated.reason == "launch_intent_repeated_after_generation"


def test_launch_guard_coalesces_sequential_replays_when_run_id_is_omitted() -> None:
    guard = RootSubagentLaunchGuard()
    arguments = {
        "name": "explorer",
        "task": "Inspect the contract.",
        "mode": "readonly",
        "workspace_view": "shared",
    }
    guard.record_spawn_result(
        arguments=arguments,
        result={"run_id": "generated-child", "state": "running"},
        tool_status="done",
        generation=1,
    )

    same_batch = guard.inspect_spawn(arguments, generation=1)
    later_round = guard.inspect_spawn(arguments, generation=2)
    another_later_round = guard.inspect_spawn(arguments, generation=3)

    assert same_batch.allow_dispatch is True
    assert same_batch.reason == "same_generation_replica"
    assert later_round.allow_dispatch is False
    assert later_round.canonical_run_id == "generated-child"
    assert later_round.stop_signal is True
    assert another_later_round.allow_dispatch is False
    assert another_later_round.stop_signal is True


def test_launch_guard_allows_a_distinct_task_to_correct_a_conflicting_run_id() -> None:
    guard = RootSubagentLaunchGuard()
    original = {
        "name": "explorer",
        "task": "Inspect the service contract.",
        "mode": "readonly",
        "workspace_view": "shared",
        "run_id": "child-a",
    }
    distinct = {**original, "task": "Inspect the incident policy."}
    guard.record_spawn_result(
        arguments=original,
        result={"run_id": "child-a", "state": "finished"},
        tool_status="done",
        generation=1,
    )
    guard.record_spawn_result(
        arguments=distinct,
        result={
            "error": "scheduler conflict",
            "error_code": "duplicate_background_subagent_run_id",
            "run_id": "child-a",
        },
        tool_status="failed",
        generation=1,
    )

    decision = guard.inspect_spawn(
        {**distinct, "run_id": "child-b"},
        generation=2,
    )

    assert decision.allow_dispatch is True
    assert decision.coalesced is False
    assert decision.stop_signal is False


def test_launch_history_is_independent_of_material_objective_revisions() -> None:
    launch_guard = RootSubagentLaunchGuard()
    progress_guard = RootSubagentProgressGuard(recent_window=4)
    arguments = {
        "name": "explorer",
        "task": "Inspect the service contract.",
        "mode": "readonly",
        "workspace_view": "shared",
        "run_id": "child-a",
    }
    launch_guard.record_spawn_result(
        arguments=arguments,
        result={"run_id": "child-a", "state": "finished"},
        tool_status="done",
        generation=1,
    )
    duplicate = launch_guard.inspect_spawn(arguments, generation=1)

    progress_guard.note_objective_transition()
    decision = launch_guard.inspect_spawn(
        {**arguments, "run_id": "child-b"},
        generation=2,
    )

    assert duplicate.coalesced is True
    assert progress_guard.objective_revision == 1
    assert decision.coalesced is True
    assert decision.canonical_run_id == "child-a"
    assert decision.stop_signal is True


def test_semantic_fingerprint_excludes_transport_and_prose_but_tracks_material_state() -> None:
    arguments = {
        "name": "implementer",
        "task": "Make the candidate change.",
        "workspace_view": "isolated",
    }
    first = subagent_semantic_outcome_fingerprint(
        arguments=arguments,
        result=_isolated_result(run_number=1),
        tool_status="done",
    )
    volatile_variant = subagent_semantic_outcome_fingerprint(
        arguments={**arguments, "task": "Paraphrased task that produced the same patch."},
        result=_isolated_result(run_number=999),
        tool_status="done",
    )
    changed_patch = subagent_semantic_outcome_fingerprint(
        arguments=arguments,
        result=_isolated_result(run_number=2, patch_sha256="b" * 64),
        tool_status="done",
    )
    changed_base = subagent_semantic_outcome_fingerprint(
        arguments=arguments,
        result=_isolated_result(run_number=3, base_commit="base-b"),
        tool_status="done",
    )

    assert volatile_variant == first
    assert changed_patch != first
    assert changed_base != first


def test_material_identity_dominates_incidental_child_envelope_fields() -> None:
    arguments = {
        "name": "implementer",
        "task": "Create the candidate.",
        "workspace_view": "isolated",
    }
    canonical = _isolated_result(run_number=1)
    canonical["material_identity_sha256"] = "c" * 64
    duplicate = _isolated_result(run_number=2, semantic_no_progress=True)
    duplicate.update(
        {
            "material_identity_sha256": "c" * 64,
            "status": "degraded",
            "error": "Transport-level report mismatch.",
            "failure_category": "final_report",
            "effects": ["delegate"],
            "touched_repo_paths": ["differently-reported.txt"],
            "material_touched_repo_paths": [],
        }
    )
    duplicate["workspace"] = {
        **duplicate["workspace"],
        "state": "duplicate",
        "parent_dirty_paths": ["unrelated-parent-file.txt"],
    }

    assert subagent_semantic_outcome_fingerprint(
        arguments=arguments,
        result=canonical,
        tool_status="done",
    ) == subagent_semantic_outcome_fingerprint(
        arguments={**arguments, "task": "Paraphrased candidate task."},
        result=duplicate,
        tool_status="failed",
    )


def test_weak_outcomes_keep_task_and_screened_finding_identity() -> None:
    arguments = {
        "name": "explorer",
        "task": "Inspect the service contract.",
        "workspace_view": "shared",
    }
    first_result = {
        "run_id": "read-1",
        "subagent": "explorer",
        "status": "success",
        "result": "The router records a route before dispatch.",
        "elapsed_ms": 10,
        "effects": ["read_workspace", "delegate"],
        "workspace": {"view": "shared"},
    }
    transport_variant = {
        **first_result,
        "run_id": "read-2",
        "subagent_session_id": "session-2",
        "elapsed_ms": 999,
        "usage": {"total_tokens": 5000},
        "effects": ["read_workspace"],
    }
    different_finding = {
        **transport_variant,
        "result": "The router dispatches before recording a route.",
    }

    first = subagent_semantic_outcome_fingerprint(
        arguments=arguments,
        result=first_result,
        tool_status="done",
    )
    assert (
        subagent_semantic_outcome_fingerprint(
            arguments=arguments,
            result=transport_variant,
            tool_status="done",
        )
        == first
    )
    assert (
        subagent_semantic_outcome_fingerprint(
            arguments=arguments,
            result=different_finding,
            tool_status="done",
        )
        != first
    )
    assert (
        subagent_semantic_outcome_fingerprint(
            arguments={**arguments, "task": "Inspect a different contract."},
            result=first_result,
            tool_status="done",
        )
        != first
    )


def test_distinct_no_change_tasks_do_not_share_identity_but_signal_stagnation() -> None:
    guard = RootSubagentProgressGuard(recent_window=8)
    observations = []
    fingerprints = []
    for index in range(1, 4):
        arguments = {
            "name": "implementer",
            "task": f"Check independent no-change objective {index}.",
            "workspace_view": "isolated",
        }
        result = {
            "run_id": f"no-change-{index}",
            "subagent": "implementer",
            "status": "degraded",
            "error": "Completed report had no matching workspace delta.",
            "failure_category": "final_report",
            "final_report_problem": "workspace_evidence_mismatch",
            "result": "No workspace delta was captured.",
            "semantic_no_progress": True,
            "workspace": {
                "view": "isolated",
                "base_commit": "base-a",
                "state": "discarded",
                "no_changes": True,
            },
            "patch_summary": {
                "files": [],
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        }
        fingerprints.append(
            subagent_semantic_outcome_fingerprint(
                arguments=arguments,
                result=result,
                tool_status="failed",
            )
        )
        observations.append(
            guard.observe(
                arguments=arguments,
                result=result,
                tool_status="failed",
            )
        )

    assert len(set(fingerprints)) == 3
    assert all(observation is not None for observation in observations)
    assert [observation.occurrences for observation in observations if observation is not None] == [
        1,
        2,
        3,
    ]
    assert all(
        observation.semantic_no_progress for observation in observations if observation is not None
    )


def test_one_wait_batch_advances_no_progress_occurrence_only_once() -> None:
    guard = RootSubagentProgressGuard(recent_window=16)

    def _outcome(index: int) -> tuple[dict[str, Any], dict[str, Any], str]:
        return (
            {
                "name": "implementer",
                "task": f"Independent no-change objective {index}.",
                "workspace_view": "isolated",
            },
            {
                "run_id": f"no-change-batch-{index}",
                "subagent": "implementer",
                "status": "no_changes",
                "result": f"No delta for objective {index}.",
                "semantic_no_progress": True,
                "workspace": {
                    "view": "isolated",
                    "base_commit": "base-a",
                    "state": "discarded",
                    "no_changes": True,
                },
            },
            "done",
        )

    first_batch = guard.observe_batch(outcomes=tuple(_outcome(index) for index in range(1, 6)))
    second_batch = guard.observe_batch(outcomes=(_outcome(6),))

    assert len(first_batch) == 5
    assert {observation.occurrences for observation in first_batch} == {1}
    assert {observation.fingerprint_occurrences for observation in first_batch} == {1}
    assert len(second_batch) == 1
    assert second_batch[0].occurrences == 2


def test_guard_resets_after_objective_transition_and_prefers_coordinator_signal() -> None:
    guard = RootSubagentProgressGuard(recent_window=8)
    arguments = {
        "name": "implementer",
        "task": "Make the candidate change.",
        "workspace_view": "isolated",
    }

    first = guard.observe(
        arguments=arguments,
        result=_isolated_result(run_number=1),
        tool_status="done",
    )
    duplicate = guard.observe(
        arguments=arguments,
        result=_isolated_result(run_number=2),
        tool_status="done",
    )
    assert first is not None and first.semantic_no_progress is False
    assert duplicate is not None and duplicate.semantic_no_progress is True
    assert duplicate.occurrences == 2

    guard.note_objective_transition()
    after_transition = guard.observe(
        arguments=arguments,
        result=_isolated_result(run_number=3),
        tool_status="done",
    )
    assert after_transition is not None
    assert after_transition.objective_revision == 1
    assert after_transition.occurrences == 1
    assert after_transition.semantic_no_progress is False

    coordinator_fresh = guard.observe(
        arguments=arguments,
        result=_isolated_result(run_number=4, semantic_no_progress=False),
        tool_status="done",
    )
    coordinator_duplicate = guard.observe(
        arguments=arguments,
        result=_isolated_result(run_number=5, semantic_no_progress=True),
        tool_status="done",
    )
    assert coordinator_fresh is not None and coordinator_fresh.semantic_no_progress is False
    assert coordinator_fresh.coordinator_signal is False
    assert coordinator_duplicate is not None
    assert coordinator_duplicate.semantic_no_progress is True
    assert coordinator_duplicate.coordinator_signal is True


def test_objective_transition_uses_tool_protocol_not_child_candidate_activity() -> None:
    guard = RootSubagentProgressGuard(recent_window=8)
    observation = guard.observe(
        arguments={
            "name": "implementer",
            "task": "Make the candidate change.",
            "workspace_view": "isolated",
        },
        result=_isolated_result(run_number=1),
        tool_status="done",
    )
    assert observation is not None
    assert not tool_result_advances_root_objective_state(
        tool_name="subagent_run",
        tool_status="done",
        result=_isolated_result(run_number=1),
        action_progress=True,
        subagent_observations=(observation,),
    )
    assert tool_result_advances_root_objective_state(
        tool_name="subagent_apply",
        tool_status="done",
        result={"ok": True, "applied_paths": ["candidate.txt"]},
        action_progress=False,
        subagent_observations=(),
    )


def test_successful_apply_records_parent_edit_once_and_noop_apply_does_not(
    tmp_path: Path,
) -> None:
    state = TurnExecutionState(execution_requested=True)
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="subagent_apply",
        arguments={"run_id": "canonical-run"},
        status="done",
        result={"ok": True, "applied_paths": ["src/candidate.py"]},
        known_verification_commands=[],
    )
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="subagent_apply",
        arguments={"run_id": "duplicate-run"},
        status="done",
        result={
            "ok": True,
            "action": "already_integrated",
            "applied_paths": [],
            "semantic_no_progress": True,
        },
        known_verification_commands=[],
    )
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="subagent_apply",
        arguments={"run_id": "failed-run"},
        status="failed",
        result={"error": "conflict", "applied_paths": ["src/ignored.py"]},
        known_verification_commands=[],
    )

    assert state.material_edit_count == 1
    assert state.material_edit_generation == 1
    assert state.material_edit_tools == {"subagent_apply"}
    assert state.touched_repo_paths == {"src/candidate.py"}
    assert state.verification_relevant_edit_generation == 1
    assert not tool_result_advances_root_objective_state(
        tool_name="subagent_apply",
        tool_status="done",
        result={
            "ok": True,
            "action": "already_integrated",
            "applied_paths": [],
            "duplicate_of": "canonical-run",
            "semantic_no_progress": True,
        },
        action_progress=False,
        subagent_observations=(),
    )
    assert tool_result_advances_root_objective_state(
        tool_name="subagent_discard",
        tool_status="done",
        result={"ok": True, "action": "discarded", "paths": ["candidate.txt"]},
        action_progress=False,
        subagent_observations=(),
    )
    assert not tool_result_advances_root_objective_state(
        tool_name="subagent_discard",
        tool_status="done",
        result={
            "ok": True,
            "action": "discarded",
            "paths": ["candidate.txt"],
            "duplicate_of": "canonical-run",
            "semantic_no_progress": True,
        },
        action_progress=False,
        subagent_observations=(),
    )


def test_wait_effect_records_only_projected_parent_visible_paths(tmp_path: Path) -> None:
    state = TurnExecutionState(execution_requested=True)

    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="subagent_wait",
        arguments={"run_id": "all"},
        status="done",
        result={
            "material_touched_repo_paths": ["src/shared.py"],
            "touched_repo_paths": ["src/shared.py"],
        },
        known_verification_commands=[],
    )

    assert state.material_edit_count == 1
    assert state.material_edit_tools == {"subagent_wait"}
    assert state.touched_repo_paths == {"src/shared.py"}


class _RepeatingSubagentClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        if tools is None:
            raise AssertionError("semantic stagnation uses a local structured final")
        self.calls += 1
        if self.calls > 12:
            raise AssertionError("root repetition guard did not contain the scripted provider")
        return LLMResponse(
            content="Launching another equivalent child.",
            tool_calls=[
                ToolCall(
                    id=f"subagent-call-{self.calls}",
                    name="subagent_run",
                    arguments={
                        "name": "implementer",
                        "task": f"Task wording variant {self.calls}.",
                        "workspace_view": "isolated",
                    },
                )
            ],
            raw={},
        )


class _ExactForegroundReplayClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        if tools is None:
            raise AssertionError("foreground replay containment uses a local final")
        self.calls += 1
        if self.calls > 3:
            raise AssertionError("equivalent foreground objective was relaunched")
        task = (
            "Open candidate.txt,\nthen edit only candidate.txt."
            if self.calls == 1
            else "  Open   candidate.txt, then edit only candidate.txt.  "
        )
        return LLMResponse(
            content="Launching the same foreground objective.",
            tool_calls=[
                ToolCall(
                    id=f"exact-foreground-replay-{self.calls}",
                    name="subagent_run",
                    arguments={
                        "name": "implementer",
                        "task": task,
                        "mode": "auto",
                        "workspace_view": "isolated",
                    },
                )
            ],
            raw={},
        )


class _SingleCapturedDuplicateClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("captured duplicate opened a provider continuation")
        if tools is None:
            raise AssertionError("captured duplicate requires the tool-enabled step")
        return LLMResponse(
            content="Inspecting the isolated candidate once.",
            tool_calls=[
                ToolCall(
                    id="captured-duplicate-call",
                    name="subagent_run",
                    arguments={
                        "name": "implementer",
                        "task": "Inspect the retained candidate once.",
                        "workspace_view": "isolated",
                    },
                )
            ],
            raw={},
        )


class _AlreadyIntegratedRunThenApplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="Checking the already-integrated candidate.",
                tool_calls=[
                    ToolCall(
                        id="already-integrated-run",
                        name="subagent_run",
                        arguments={
                            "name": "implementer",
                            "task": "Inspect the already-integrated candidate.",
                            "workspace_view": "isolated",
                        },
                    )
                ],
                raw={},
            )
        if self.calls == 2:
            return LLMResponse(
                content="Applying through the typed lifecycle operation.",
                tool_calls=[
                    ToolCall(
                        id="already-integrated-followup-apply",
                        name="subagent_apply",
                        arguments={
                            "run_id": "duplicate-run",
                            "acknowledge_incomplete": False,
                        },
                    )
                ],
                raw={},
            )
        if self.calls == 3:
            return LLMResponse(content="Lifecycle replay completed.", tool_calls=[], raw={})
        raise AssertionError("already-integrated lifecycle did not finalize")


class _MixedCapturedDuplicateBatchClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="Checking two independent candidates.",
                tool_calls=[
                    ToolCall(
                        id="captured-duplicate-a",
                        name="subagent_run",
                        arguments={
                            "name": "implementer",
                            "task": "Inspect candidate A.",
                            "workspace_view": "isolated",
                        },
                    ),
                    ToolCall(
                        id="captured-duplicate-b",
                        name="subagent_run",
                        arguments={
                            "name": "implementer",
                            "task": "Inspect candidate B.",
                            "workspace_view": "isolated",
                        },
                    ),
                ],
                raw={},
            )
        if self.calls == 2:
            return LLMResponse(content="Both candidates inspected.", tool_calls=[], raw={})
        raise AssertionError("mixed duplicate batch did not finalize")


class _RepeatingAlreadyIntegratedApplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        if tools is None:
            raise AssertionError("lifecycle replay containment uses a local final")
        self.calls += 1
        if self.calls > 2:
            raise AssertionError("identical already-integrated apply was redispatched")
        return LLMResponse(
            content="Applying the same already-integrated candidate.",
            tool_calls=[
                ToolCall(
                    id=f"already-integrated-apply-{self.calls}",
                    name="subagent_apply",
                    arguments={
                        "run_id": "duplicate-run",
                        "acknowledge_incomplete": False,
                    },
                )
            ],
            raw={},
        )


class _RetryThenDistinctApplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 4:
            return LLMResponse(content="Lifecycle operations completed.", tool_calls=[], raw={})
        if self.calls > 4:
            raise AssertionError("retry/distinct apply sequence did not finalize")
        return LLMResponse(
            content="Retrying or changing the typed lifecycle operation.",
            tool_calls=[
                ToolCall(
                    id=f"retry-or-distinct-apply-{self.calls}",
                    name="subagent_apply",
                    arguments={
                        "run_id": "duplicate-run",
                        "acknowledge_incomplete": self.calls == 3,
                    },
                )
            ],
            raw={},
        )


class _ApplyDiscardApplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 4:
            return LLMResponse(content="Provider lifecycle state confirmed.", tool_calls=[], raw={})
        if self.calls > 4:
            raise AssertionError("apply/discard/apply sequence did not finalize")
        tool_name = "subagent_discard" if self.calls == 2 else "subagent_apply"
        return LLMResponse(
            content="Checking the candidate lifecycle state.",
            tool_calls=[
                ToolCall(
                    id=f"apply-discard-apply-{self.calls}",
                    name=tool_name,
                    arguments={
                        "run_id": "duplicate-run",
                        **(
                            {"acknowledge_incomplete": False}
                            if tool_name == "subagent_apply"
                            else {}
                        ),
                    },
                )
            ],
            raw={},
        )


class _SameBatchForegroundReplicaClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 1:
            arguments = {
                "name": "implementer",
                "task": "Open candidate.txt, then edit only candidate.txt.",
                "mode": "auto",
                "workspace_view": "isolated",
            }
            return LLMResponse(
                content="Launching two intentional replicas.",
                tool_calls=[
                    ToolCall(
                        id="same-batch-replica-a",
                        name="subagent_run",
                        arguments=dict(arguments),
                    ),
                    ToolCall(
                        id="same-batch-replica-b",
                        name="subagent_run",
                        arguments=dict(arguments),
                    ),
                ],
                raw={},
            )
        if self.calls == 2:
            return LLMResponse(content="Both replicas completed.", tool_calls=[], raw={})
        raise AssertionError("same-batch replicas did not finalize")


class _RepeatingWithNoopApplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        if tools is None:
            raise AssertionError("semantic stagnation uses a local structured final")
        self.calls += 1
        if self.calls > 12:
            raise AssertionError("no-op apply loop was not contained")
        return LLMResponse(
            content="Repeating a child and an already-integrated apply.",
            tool_calls=[
                ToolCall(
                    id=f"alternating-run-{self.calls}",
                    name="subagent_run",
                    arguments={
                        "name": "implementer",
                        "task": f"Equivalent candidate wording {self.calls}.",
                        "workspace_view": "isolated",
                    },
                ),
                ToolCall(
                    id=f"alternating-apply-{self.calls}",
                    name="subagent_apply",
                    arguments={"run_id": f"duplicate-{self.calls}"},
                ),
            ],
            raw={},
        )


class _ResolvingApplyClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        if tools is None:
            raise AssertionError("the resolving turn should complete normally")
        self.calls += 1
        if self.calls > 4:
            raise AssertionError("real apply did not resolve the pending stagnation decision")
        if self.calls == 4:
            return LLMResponse(content="Applied the existing candidate.", tool_calls=[], raw={})
        tool_calls = [
            ToolCall(
                id=f"resolving-run-{self.calls}",
                name="subagent_run",
                arguments={
                    "name": "implementer",
                    "task": f"Equivalent candidate wording {self.calls}.",
                    "workspace_view": "isolated",
                },
            )
        ]
        if self.calls == 3:
            tool_calls.append(
                ToolCall(
                    id="resolving-apply",
                    name="subagent_apply",
                    arguments={"run_id": "canonical"},
                )
            )
        return LLMResponse(
            content="Resolving the repeated candidate.",
            tool_calls=tool_calls,
            raw={},
        )


class _RepeatingBackgroundSubagentClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        if tools is None:
            raise AssertionError("semantic stagnation uses a local structured final")
        self.calls += 1
        if self.calls > 16:
            raise AssertionError("background repetition guard did not contain the provider")
        run_number = (self.calls + 1) // 2
        if self.calls % 2:
            name = "subagent_spawn"
            arguments = {
                "name": "implementer",
                "task": f"Background task wording variant {run_number}.",
                "workspace_view": "isolated",
            }
        else:
            name = "subagent_wait"
            arguments = {"run_id": f"background-{run_number}"}
        return LLMResponse(
            content="Continuing background delegation.",
            tool_calls=[
                ToolCall(
                    id=f"background-call-{self.calls}",
                    name=name,
                    arguments=arguments,
                )
            ],
            raw={},
        )


class _MixedWaitClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="Collecting the mixed child batch.",
                tool_calls=[
                    ToolCall(
                        id="mixed-wait-call",
                        name="subagent_wait",
                        arguments={"run_id": "all"},
                    )
                ],
                raw={},
            )
        if self.calls > 4:
            raise AssertionError("mixed wait turn did not finalize")
        return LLMResponse(content="Mixed child batch collected.", tool_calls=[], raw={})


def test_captured_duplicate_terminalizes_without_provider_continuation(
    tmp_path: Path,
) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original = session.tools["subagent_run"]
    dispatched_calls: list[dict[str, Any]] = []

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched_calls.append(dict(arguments))
        return _captured_duplicate_result()

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original.description,
        parameters=original.parameters,
        run=_fake_subagent_run,
        metadata=original.metadata,
    )
    client = _SingleCapturedDuplicateClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Inspect the captured candidate once.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 1
    assert len(dispatched_calls) == 1
    assert len(_event_payloads(log_path, "tool_call")) == 1
    tool_results = _event_payloads(log_path, "tool_result")
    assert len(tool_results) == 1
    assert tool_results[0]["result"]["semantic_no_progress"] is True
    terminalized = _event_payloads(
        log_path,
        "captured_duplicate_subagent_turn_terminalized",
    )
    assert len(terminalized) == 1
    assert terminalized[0]["run_id"] == "run-2"
    finals = _event_payloads(log_path, "final")
    assert len(finals) == 1
    assert finals[0]["controller_interventions_total"] == 0
    assert json.loads(finals[0]["content"])["canonical_state"] == "captured"
    assert _event_payloads(log_path, "llm_call_retry") == []
    assert all(
        payload["headline_counted"] is False
        for payload in _event_payloads(log_path, "controller_intervention")
    )
    assert _event_payloads(log_path, "root_subagent_foreground_replay_coalesced") == []
    assert _event_payloads(log_path, "root_subagent_foreground_replay_blocked") == []
    assert _event_payloads(log_path, "root_subagent_semantic_repetition_detected") == []
    assert _event_payloads(log_path, "root_subagent_semantic_repetition_nudge") == []
    assert _event_payloads(log_path, "root_subagent_semantic_repetition_backstop") == []
    assert _event_payloads(log_path, "forced_final_summary_requested") == []
    assert _event_payloads(log_path, "optional_finalization_failure_fallback") == []


def test_already_integrated_duplicate_continues_to_apply(tmp_path: Path) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original_run = session.tools["subagent_run"]
    original_apply = session.tools["subagent_apply"]
    run_calls: list[dict[str, Any]] = []
    apply_calls: list[dict[str, Any]] = []

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        run_calls.append(dict(arguments))
        result = _captured_duplicate_result()
        result.update(
            {
                "run_id": "duplicate-run",
                "canonical_state": "applied",
                "already_integrated": True,
            }
        )
        return result

    def _fake_subagent_apply(arguments: dict[str, Any]) -> dict[str, Any]:
        apply_calls.append(dict(arguments))
        return _already_integrated_apply_result(str(arguments["run_id"]))

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original_run.description,
        parameters=original_run.parameters,
        run=_fake_subagent_run,
        metadata=original_run.metadata,
    )
    session.tools["subagent_apply"] = ToolDef(
        name="subagent_apply",
        description=original_apply.description,
        parameters=original_apply.parameters,
        run=_fake_subagent_apply,
        metadata=original_apply.metadata,
    )
    client = _AlreadyIntegratedRunThenApplyClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Replay and apply the integrated candidate.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 3
    assert len(run_calls) == 1
    assert apply_calls == [{"run_id": "duplicate-run", "acknowledge_incomplete": False}]
    assert [payload["name"] for payload in _event_payloads(log_path, "tool_result")] == [
        "subagent_run",
        "subagent_apply",
    ]
    assert _event_payloads(log_path, "captured_duplicate_subagent_turn_terminalized") == []


def test_mixed_duplicate_batch_does_not_hide_distinct_sibling_task(tmp_path: Path) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original = session.tools["subagent_run"]
    dispatched_calls: list[dict[str, Any]] = []
    dispatch_lock = Lock()

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        with dispatch_lock:
            dispatched_calls.append(dict(arguments))
            run_number = len(dispatched_calls) + 1
        result = _captured_duplicate_result(
            run_number=run_number,
            canonical_run_id=f"canonical-{run_number}",
        )
        result["material_identity_sha256"] = f"{run_number:064x}"
        return result

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original.description,
        parameters=original.parameters,
        run=_fake_subagent_run,
        metadata=original.metadata,
    )
    # Exercise the thread-pool prelaunch fallback so the replacement tool is
    # used for both intentional same-response calls.
    session.child_scheduler = None
    client = _MixedCapturedDuplicateBatchClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Inspect both independent candidates.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 2
    assert {call["task"] for call in dispatched_calls} == {
        "Inspect candidate A.",
        "Inspect candidate B.",
    }
    assert len(_event_payloads(log_path, "tool_result")) == 2
    assert _event_payloads(log_path, "captured_duplicate_subagent_turn_terminalized") == []


def test_unlimited_root_turn_stops_repeated_semantic_subagent_outcomes(
    tmp_path: Path,
) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original = session.tools["subagent_run"]
    tool_calls: list[dict[str, Any]] = []

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(dict(arguments))
        return _isolated_result(run_number=len(tool_calls))

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original.description,
        parameters=original.parameters,
        run=_fake_subagent_run,
        metadata=original.metadata,
    )
    client = _RepeatingSubagentClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement the candidate through an isolated subagent.")
        log_path = session.store.path
        budget_resolution = session.step_budget_runtime.last_resolution
    finally:
        session.close()

    assert exit_code == 1
    assert budget_resolution is not None
    assert budget_resolution.unlimited is True
    assert budget_resolution.resolved_max_steps is None
    assert client.calls == 3
    assert len(tool_calls) == 3

    detections = _event_payloads(log_path, "root_subagent_semantic_repetition_detected")
    assert [payload["occurrences"] for payload in detections] == [2, 3]
    assert all(payload["semantic_no_progress"] is True for payload in detections)
    nudges = _event_payloads(log_path, "root_subagent_semantic_repetition_nudge")
    assert len(nudges) == 1
    assert nudges[0]["threshold"] == 2
    backstops = _event_payloads(log_path, "root_subagent_semantic_repetition_backstop")
    assert len(backstops) == 1
    assert backstops[0]["threshold"] == 3
    assert backstops[0]["termination_kind"] == "execution_guard_stagnation"
    assert backstops[0]["material_edit_count"] == 0
    assert backstops[0]["material_edit_generation"] == 0
    assert backstops[0]["touched_repo_paths"] == []
    forced = _event_payloads(log_path, "forced_final_summary_requested")
    assert forced[-1]["termination_kind"] == "execution_guard_stagnation"
    assert forced[-1]["max_steps"] is None


def test_foreground_replay_is_coalesced_then_stopped_without_another_child(
    tmp_path: Path,
) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original = session.tools["subagent_run"]
    dispatched_calls: list[dict[str, Any]] = []

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched_calls.append(dict(arguments))
        return _isolated_result(run_number=1, semantic_no_progress=False)

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original.description,
        parameters=original.parameters,
        run=_fake_subagent_run,
        metadata=original.metadata,
    )
    client = _ExactForegroundReplayClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement the candidate through an isolated subagent.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert client.calls == 3
    assert len(dispatched_calls) == 1

    coalesced = _event_payloads(log_path, "root_subagent_foreground_replay_coalesced")
    assert len(coalesced) == 1
    assert coalesced[0]["stop_signal"] is False
    blocked = _event_payloads(log_path, "root_subagent_foreground_replay_blocked")
    assert len(blocked) == 1
    assert blocked[0]["stop_signal"] is True

    tool_results = _event_payloads(log_path, "tool_result")
    assert tool_results[1]["result"]["run_id"] == "run-1"
    assert tool_results[1]["result"]["canonical_run_id"] == "run-1"
    assert tool_results[1]["result"]["semantic_no_progress"] is True
    assert tool_results[2]["result"]["error_code"] == ("equivalent_foreground_subagent_replay")
    backstops = _event_payloads(log_path, "root_subagent_semantic_repetition_backstop")
    assert len(backstops) == 1
    assert backstops[0]["source"] == "foreground_launch_intent"
    assert backstops[0]["termination_trigger"] == (
        "foreground_launch_replay_after_semantic_no_progress"
    )


def test_identical_already_integrated_apply_is_stopped_before_redispatch(
    tmp_path: Path,
) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original_apply = session.tools["subagent_apply"]
    dispatched_calls: list[dict[str, Any]] = []

    def _fake_subagent_apply(arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched_calls.append(dict(arguments))
        return _already_integrated_apply_result(str(arguments["run_id"]))

    session.tools["subagent_apply"] = ToolDef(
        name="subagent_apply",
        description=original_apply.description,
        parameters=original_apply.parameters,
        run=_fake_subagent_apply,
        metadata=original_apply.metadata,
    )
    client = _RepeatingAlreadyIntegratedApplyClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Apply the retained isolated candidate.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert client.calls == 2
    assert dispatched_calls == [{"run_id": "duplicate-run", "acknowledge_incomplete": False}]
    assert len(_event_payloads(log_path, "tool_call")) == 2
    blocked = _event_payloads(log_path, "root_subagent_lifecycle_replay_blocked")
    assert len(blocked) == 1
    assert blocked[0]["lifecycle_tool"] == "subagent_apply"
    assert blocked[0]["source"] == "synchronous_lifecycle_operation"
    assert blocked[0]["stop_signal"] is True

    tool_results = _event_payloads(log_path, "tool_result")
    assert len(tool_results) == 2
    assert tool_results[0]["result"]["action"] == "already_integrated"
    assert tool_results[1]["result"]["coalesced"] is True
    assert tool_results[1]["result"]["dispatch_blocked"] is True
    assert tool_results[1]["result"]["error_code"] == ("equivalent_subagent_lifecycle_replay")
    backstops = _event_payloads(log_path, "root_subagent_semantic_repetition_backstop")
    assert len(backstops) == 1
    assert backstops[0]["source"] == "synchronous_lifecycle_operation"
    assert backstops[0]["termination_trigger"] == ("synchronous_lifecycle_replay_after_no_progress")


def test_failed_retry_and_distinct_apply_arguments_still_dispatch(tmp_path: Path) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original_apply = session.tools["subagent_apply"]
    dispatched_calls: list[dict[str, Any]] = []

    def _fake_subagent_apply(arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched_calls.append(dict(arguments))
        if len(dispatched_calls) == 1:
            return {"ok": False, "error": "transient provider failure"}
        return _already_integrated_apply_result(str(arguments["run_id"]))

    session.tools["subagent_apply"] = ToolDef(
        name="subagent_apply",
        description=original_apply.description,
        parameters=original_apply.parameters,
        run=_fake_subagent_apply,
        metadata=original_apply.metadata,
    )
    client = _RetryThenDistinctApplyClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Retry and then acknowledge the isolated candidate.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 4
    assert dispatched_calls == [
        {"run_id": "duplicate-run", "acknowledge_incomplete": False},
        {"run_id": "duplicate-run", "acknowledge_incomplete": False},
        {"run_id": "duplicate-run", "acknowledge_incomplete": True},
    ]
    assert _event_payloads(log_path, "root_subagent_lifecycle_replay_blocked") == []
    assert _event_payloads(log_path, "root_subagent_semantic_repetition_backstop") == []


def test_successful_discard_invalidates_already_integrated_apply_replay(
    tmp_path: Path,
) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original_apply = session.tools["subagent_apply"]
    original_discard = session.tools["subagent_discard"]
    apply_calls: list[dict[str, Any]] = []
    discard_calls: list[dict[str, Any]] = []

    def _fake_subagent_apply(arguments: dict[str, Any]) -> dict[str, Any]:
        apply_calls.append(dict(arguments))
        if len(apply_calls) == 1:
            return _already_integrated_apply_result(str(arguments["run_id"]))
        return {
            "ok": False,
            "run_id": str(arguments["run_id"]),
            "error": "Workspace already discarded.",
            "error_code": "workspace_already_discarded",
        }

    def _fake_subagent_discard(arguments: dict[str, Any]) -> dict[str, Any]:
        discard_calls.append(dict(arguments))
        return {
            "ok": True,
            "run_id": str(arguments["run_id"]),
            "action": "discarded",
            "duplicate_of": "canonical-run",
            "canonical_run_id": "canonical-run",
            "semantic_no_progress": True,
        }

    session.tools["subagent_apply"] = ToolDef(
        name="subagent_apply",
        description=original_apply.description,
        parameters=original_apply.parameters,
        run=_fake_subagent_apply,
        metadata=original_apply.metadata,
    )
    session.tools["subagent_discard"] = ToolDef(
        name="subagent_discard",
        description=original_discard.description,
        parameters=original_discard.parameters,
        run=_fake_subagent_discard,
        metadata=original_discard.metadata,
    )
    client = _ApplyDiscardApplyClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Confirm lifecycle behavior after discarding the alias.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 4
    assert apply_calls == [
        {"run_id": "duplicate-run", "acknowledge_incomplete": False},
        {"run_id": "duplicate-run", "acknowledge_incomplete": False},
    ]
    assert discard_calls == [{"run_id": "duplicate-run"}]
    assert _event_payloads(log_path, "root_subagent_lifecycle_replay_blocked") == []
    tool_results = _event_payloads(log_path, "tool_result")
    assert [payload["name"] for payload in tool_results] == [
        "subagent_apply",
        "subagent_discard",
        "subagent_apply",
    ]
    assert tool_results[-1]["result"]["error_code"] == "workspace_already_discarded"


def test_same_assistant_batch_foreground_replicas_both_dispatch(tmp_path: Path) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original = session.tools["subagent_run"]
    dispatched_calls: list[dict[str, Any]] = []
    dispatch_lock = Lock()

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        with dispatch_lock:
            dispatched_calls.append(dict(arguments))
            run_number = len(dispatched_calls)
        return _isolated_result(
            run_number=run_number,
            patch_sha256=("a" if run_number == 1 else "b") * 64,
        )

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original.description,
        parameters=original.parameters,
        run=_fake_subagent_run,
        metadata=original.metadata,
    )
    # Exercise the thread-pool prelaunch fallback so the test proves the turn
    # loop does not reclassify an already-dispatched same-response replica.
    session.child_scheduler = None
    client = _SameBatchForegroundReplicaClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Run two intentional replicas in one response.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 2
    assert len(dispatched_calls) == 2
    assert _event_payloads(log_path, "root_subagent_foreground_replay_coalesced") == []
    assert _event_payloads(log_path, "root_subagent_foreground_replay_blocked") == []
    tool_results = _event_payloads(log_path, "tool_result")
    assert [payload["result"]["run_id"] for payload in tool_results] == ["run-1", "run-2"]


def test_noop_apply_cannot_reset_repeated_child_progress_guard(tmp_path: Path) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original_run = session.tools["subagent_run"]
    original_apply = session.tools["subagent_apply"]
    run_calls: list[dict[str, Any]] = []
    apply_calls: list[dict[str, Any]] = []

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        run_calls.append(dict(arguments))
        return _isolated_result(run_number=len(run_calls))

    def _fake_subagent_apply(arguments: dict[str, Any]) -> dict[str, Any]:
        apply_calls.append(dict(arguments))
        return {
            "ok": True,
            "action": "already_integrated",
            "applied_paths": [],
            "semantic_no_progress": True,
        }

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original_run.description,
        parameters=original_run.parameters,
        run=_fake_subagent_run,
        metadata=original_run.metadata,
    )
    session.tools["subagent_apply"] = ToolDef(
        name="subagent_apply",
        description=original_apply.description,
        parameters=original_apply.parameters,
        run=_fake_subagent_apply,
        metadata=original_apply.metadata,
    )
    client = _RepeatingWithNoopApplyClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement through repeated isolated candidates.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert client.calls == 3
    assert len(run_calls) == 3
    assert len(apply_calls) == 3
    detections = _event_payloads(log_path, "root_subagent_semantic_repetition_detected")
    assert [payload["occurrences"] for payload in detections] == [2, 3]
    backstops = _event_payloads(log_path, "root_subagent_semantic_repetition_backstop")
    assert len(backstops) == 1
    assert backstops[0]["material_edit_count"] == 0
    assert backstops[0]["touched_repo_paths"] == []


def test_real_apply_clears_same_batch_pending_stagnation(tmp_path: Path) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original_run = session.tools["subagent_run"]
    original_apply = session.tools["subagent_apply"]
    run_calls: list[dict[str, Any]] = []

    def _fake_subagent_run(arguments: dict[str, Any]) -> dict[str, Any]:
        run_calls.append(dict(arguments))
        return _isolated_result(run_number=len(run_calls))

    session.tools["subagent_run"] = ToolDef(
        name="subagent_run",
        description=original_run.description,
        parameters=original_run.parameters,
        run=_fake_subagent_run,
        metadata=original_run.metadata,
    )
    session.tools["subagent_apply"] = ToolDef(
        name="subagent_apply",
        description=original_apply.description,
        parameters=original_apply.parameters,
        run=lambda _arguments: {"ok": True, "applied_paths": ["candidate.txt"]},
        metadata=original_apply.metadata,
    )
    client = _ResolvingApplyClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Apply an existing isolated candidate.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 4
    assert len(run_calls) == 3
    assert _event_payloads(log_path, "root_subagent_semantic_repetition_backstop") == []


def test_mixed_wait_projects_only_shared_child_paths_to_parent_state(tmp_path: Path) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=False)
    original_wait = session.tools["subagent_wait"]

    def _fake_subagent_wait(_arguments: dict[str, Any]) -> dict[str, Any]:
        isolated = _isolated_result(run_number=1, patch_sha256="a" * 64)
        isolated["touched_repo_paths"] = ["private.txt"]
        isolated["material_touched_repo_paths"] = ["private.txt"]
        isolated["patch_summary"] = {
            **isolated["patch_summary"],
            "files": ["private.txt"],
        }
        shared = _isolated_result(run_number=2, patch_sha256="b" * 64)
        shared["workspace"] = {
            **shared["workspace"],
            "view": "shared",
        }
        shared["touched_repo_paths"] = ["shared.txt"]
        shared["material_touched_repo_paths"] = ["shared.txt"]
        shared["patch_summary"] = {
            **shared["patch_summary"],
            "files": ["shared.txt"],
        }
        return {
            "results": {
                "isolated-child": isolated,
                "shared-child": shared,
            },
            "pending_run_ids": [],
            "wait_pending": False,
        }

    session.tools["subagent_wait"] = ToolDef(
        name="subagent_wait",
        description=original_wait.description,
        parameters=original_wait.parameters,
        run=_fake_subagent_wait,
        metadata=original_wait.metadata,
    )
    client = _MixedWaitClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Collect the completed background implementation batch.")
        touched_paths = set(session.workspace_touched_paths)
    finally:
        session.close()

    assert exit_code == 0
    assert client.calls == 2
    assert touched_paths == {"shared.txt"}


def test_background_wait_has_semantic_guard_parity_without_counting_spawn_acks(
    tmp_path: Path,
) -> None:
    session = _create_subagent_test_session(tmp_path, semantic_guard=True)
    original_spawn = session.tools["subagent_spawn"]
    original_wait = session.tools["subagent_wait"]
    spawn_calls: list[dict[str, Any]] = []
    wait_calls: list[dict[str, Any]] = []

    def _fake_subagent_spawn(arguments: dict[str, Any]) -> dict[str, Any]:
        spawn_calls.append(dict(arguments))
        return {
            "ok": True,
            "run_id": f"background-{len(spawn_calls)}",
            "state": "running",
        }

    def _fake_subagent_wait(arguments: dict[str, Any]) -> dict[str, Any]:
        wait_calls.append(dict(arguments))
        run_number = len(wait_calls)
        run_id = str(arguments["run_id"])
        return {
            "results": {
                run_id: _isolated_result(
                    run_number=run_number,
                    semantic_no_progress=True if run_number > 1 else None,
                )
            },
            "pending_run_ids": [],
            "wait_pending": False,
        }

    session.tools["subagent_spawn"] = ToolDef(
        name="subagent_spawn",
        description=original_spawn.description,
        parameters=original_spawn.parameters,
        run=_fake_subagent_spawn,
        metadata=original_spawn.metadata,
    )
    session.tools["subagent_wait"] = ToolDef(
        name="subagent_wait",
        description=original_wait.description,
        parameters=original_wait.parameters,
        run=_fake_subagent_wait,
        metadata=original_wait.metadata,
    )
    client = _RepeatingBackgroundSubagentClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement through background isolated subagents.")
        log_path = session.store.path
        budget_resolution = session.step_budget_runtime.last_resolution
    finally:
        session.close()

    assert exit_code == 1
    assert budget_resolution is not None and budget_resolution.unlimited is True
    assert budget_resolution.resolved_max_steps is None
    assert client.calls == 6
    assert len(spawn_calls) == 3
    assert len(wait_calls) == 3

    detections = _event_payloads(log_path, "root_subagent_semantic_repetition_detected")
    assert [payload["occurrences"] for payload in detections] == [2, 3]
    assert [payload["tool_call_id"] for payload in detections] == [
        "background-call-4",
        "background-call-6",
    ]
    nudges = _event_payloads(log_path, "root_subagent_semantic_repetition_nudge")
    assert len(nudges) == 1
    backstops = _event_payloads(log_path, "root_subagent_semantic_repetition_backstop")
    assert len(backstops) == 1
    assert backstops[0]["termination_kind"] == "execution_guard_stagnation"
