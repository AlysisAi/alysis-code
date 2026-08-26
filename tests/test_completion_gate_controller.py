from __future__ import annotations

from alysis_code.agent.verification import _completion_gate_nudge_message
from alysis_code.agent_loop import (
    CompletionGateControllerState,
    CompletionGateDecisionKind,
    TurnExecutionState,
    build_completion_gate_snapshot,
    decide_completion_gate,
    normalize_completion_gate_failure_signature,
    record_completion_gate_decision,
)

_LIVE_BG_LINE_2 = (
    "- You have 2 background process(es) started with shell_background; they are terminated "
    "when this run ends. If the task requires a server/daemon to still be running after you "
    "finish, start it with shell_service_start (durable) instead, and re-verify."
)


def _snapshot(
    *,
    stage: str = "no_material_edits",
    material_edit_count: int = 0,
    problems: list[str] | None = None,
    missing: set[str] | None = None,
    failures: dict[str, str] | None = None,
):
    return build_completion_gate_snapshot(
        stage=stage,
        problems=problems if problems is not None else [stage],
        material_edit_count=material_edit_count,
        material_edit_tools={"fs_write"} if material_edit_count else set(),
        touched_repo_paths={"src/app.py"} if material_edit_count else set(),
        verification_relevant_edit_generation=1 if material_edit_count else 0,
        last_successful_verification_generation=None,
        expected_verification_commands={"pytest -q"},
        covered_verification_commands=set(),
        missing_verification_commands=missing if missing is not None else {"pytest -q"},
        failed_verification_command_snippets=failures or {},
        verification_coverage_stale=False,
        last_verification_passed=None,
        verification_expected=True,
        final_text="Implemented it.",
        repo_tool_activity_observed=True,
    )


def test_controller_allows_final_when_no_problems() -> None:
    state = CompletionGateControllerState()
    snapshot = build_completion_gate_snapshot(
        stage="complete",
        problems=[],
        material_edit_count=1,
        material_edit_tools={"fs_write"},
        touched_repo_paths={"src/app.py"},
        verification_relevant_edit_generation=1,
        last_successful_verification_generation=1,
        expected_verification_commands={"pytest -q"},
        covered_verification_commands={"pytest -q"},
        missing_verification_commands=set(),
        failed_verification_command_snippets={},
        verification_coverage_stale=False,
        last_verification_passed=True,
        verification_expected=True,
        final_text="Implemented and verified.",
        repo_tool_activity_observed=True,
    )

    decision = decide_completion_gate(state, snapshot)

    assert decision.kind == CompletionGateDecisionKind.ALLOW_FINAL
    assert decision.reason == "requirements_satisfied"


def test_open_problems_nudge_once_then_allow_final() -> None:
    state = CompletionGateControllerState()
    snapshot = _snapshot()

    first = decide_completion_gate(state, snapshot)
    record_completion_gate_decision(state, first)
    second = decide_completion_gate(state, snapshot)

    assert first.kind == CompletionGateDecisionKind.NUDGE_ONCE
    assert first.reason == "advisory_checklist_needed"
    assert state.checklist_sent is True
    assert second.kind == CompletionGateDecisionKind.ALLOW_FINAL
    assert second.reason == "advisory_checklist_already_sent"
    assert second.problems == ("no_material_edits",)


def test_missing_verification_recommends_text_only_action_without_forced_tool() -> None:
    state = CompletionGateControllerState()
    decision = decide_completion_gate(
        state,
        _snapshot(stage="verification_not_attempted", material_edit_count=1),
    )

    payload = decision.as_payload()

    assert decision.kind == CompletionGateDecisionKind.NUDGE_ONCE
    assert payload["recommended_action"] == "run_verification"
    assert payload["preferred_tool_names"] == []


def test_failed_verification_recommends_repair_without_escalating_to_test_discovery() -> None:
    state = CompletionGateControllerState()
    snapshot = _snapshot(
        stage="verification_failed",
        material_edit_count=1,
        failures={"pytest -q": "AssertionError: wrong"},
    )

    first = decide_completion_gate(state, snapshot)
    record_completion_gate_decision(state, first)
    second = decide_completion_gate(state, snapshot)

    assert first.as_payload()["recommended_action"] == "repair_verification_failure"
    assert first.as_payload()["preferred_tool_names"] == []
    assert second.kind == CompletionGateDecisionKind.ALLOW_FINAL
    assert second.as_payload()["preferred_tool_names"] == []


def test_budget_exhaustion_does_not_create_gate_terminal_decision() -> None:
    state = CompletionGateControllerState()
    decision = decide_completion_gate(state, _snapshot(), budget_exhausted=True)

    assert decision.kind == CompletionGateDecisionKind.NUDGE_ONCE
    assert decision.reason == "advisory_checklist_needed"


def test_turn_execution_state_payload_tracks_checklist_controller() -> None:
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={"pytest -q"},
    )
    state.note_material_edit()
    state.record_diff_review()
    state.note_material_edit()

    payload = state.as_payload()

    assert payload["execution_requested"] is True
    assert payload["material_edit_count"] == 2
    assert payload["material_edit_generation"] == 2
    assert payload["last_diff_review_generation"] == 1
    assert payload["diff_review_stale"] is True
    assert payload["completion_gate_repair_attempts"] == 0
    assert payload["completion_gate_no_material_edits_repair_attempts"] == 0
    assert payload["completion_gate_missing_verify_repair_attempts"] == 0
    assert payload["completion_gate_failed_verify_repair_attempts"] == 0
    assert payload["completion_gate_controller"]["total_rejected_finalizations"] == 0
    assert payload["completion_gate_controller"]["checklist_sent"] is False
    assert "completion_gate_episode_id" not in payload
    assert "completion_gate_stagnant_attempt_count" not in payload
    assert "completion_gate_consecutive_no_progress_rejections" not in payload


def test_recorded_nudge_updates_controller_payload_without_episode_state() -> None:
    state = CompletionGateControllerState()
    decision = decide_completion_gate(state, _snapshot())
    record_completion_gate_decision(state, decision)

    payload = state.as_payload()

    assert payload["checklist_sent"] is True
    assert "last_episode_id" not in payload
    assert "stagnant_attempt_count" not in payload
    assert "consecutive_no_progress_rejections" not in payload
    assert payload["last_snapshot_payload"]["stage"] == "no_material_edits"
    assert payload["last_decision_kind"] == "NUDGE_ONCE"


def test_failure_signature_normalization_keeps_distinct_errors() -> None:
    first = normalize_completion_gate_failure_signature(
        "FAILED /tmp/run-123/test.py at 2026-06-18T10:11:12Z after 1.23s: NameError"
    )
    second = normalize_completion_gate_failure_signature(
        "FAILED /tmp/run-999/test.py at 2026-06-18T10:12:13Z after 2.34s: AssertionError"
    )

    assert "<temp-path>" in first
    assert "<timestamp>" in first
    assert "<duration>" in first
    assert first != second


def test_completion_gate_nudge_live_background_warning_is_one_shot_only() -> None:
    one_shot_message = _completion_gate_nudge_message(
        ["verification_not_attempted"],
        one_shot_execution=True,
        live_background_processes=2,
    )
    interactive_message = _completion_gate_nudge_message(
        ["verification_not_attempted"],
        one_shot_execution=False,
        live_background_processes=2,
    )
    no_live_process_message = _completion_gate_nudge_message(
        ["verification_not_attempted"],
        one_shot_execution=True,
        live_background_processes=0,
    )

    assert one_shot_message.splitlines().count(_LIVE_BG_LINE_2) == 1
    assert _LIVE_BG_LINE_2 not in interactive_message.splitlines()
    assert _LIVE_BG_LINE_2 not in no_live_process_message.splitlines()
