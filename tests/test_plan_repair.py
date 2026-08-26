"""Malformed planner output driven through the validate-and-repair loop.

Every test here feeds a deliberately broken model response into the real planner
path and asserts the same four properties: the repair retries actually happen,
what the host had to salvage is recorded, the loop is bounded, and a terminal
state is always reached.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx

from alysis_code.config import AppConfig
from alysis_code.plan_assistant import run_planner_turn
from alysis_code.plan_repair import (
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_EXECUTION_READY,
    TERMINAL_FAILED,
    TERMINAL_FORCED_DRAFT,
    TERMINAL_HOST_REPAIRED,
    TERMINAL_VALIDATED,
    ClarificationLoopTracker,
    PlannerRepairReport,
    apply_plan_status,
    assess_plan_status,
    clarification_goal_key,
    host_repaired_field_paths,
    plan_repair_event_payload,
    plan_repair_metadata,
    plan_status,
    plan_update_proposes_task_work,
    record_plan_repair,
    repair_strictness_instruction,
    resolve_plan_repair_policy,
)

TERMINAL_STATES = {
    TERMINAL_VALIDATED,
    TERMINAL_HOST_REPAIRED,
    TERMINAL_FORCED_DRAFT,
    TERMINAL_FAILED,
}


def _base_plan() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": "run_1",
        "created_at": "2026-07-28T00:00:00+00:00",
        "updated_at": "2026-07-28T00:00:00+00:00",
        "project_goal": "Harden the planner payload path",
        "summary": "Initial summary",
        # Non-empty so the plan is not "thin": a thin plan triggers the separate
        # question-repair round trip, which would add calls unrelated to what
        # these tests measure.
        "requirements": ["Bound the planner repair loop."],
        "tasks": [],
        "assets": [],
    }


def _cfg() -> AppConfig:
    return AppConfig(base_url="https://example.com/v1", model="planner-model")


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _scripted_transport(
    responses: list[str],
    *,
    prompts: list[str] | None = None,
) -> httpx.MockTransport:
    """Serve ``responses`` in order; the last one repeats if the loop asks again."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if prompts is not None:
            prompts.append(
                "\n".join(
                    str(message.get("content") or "")
                    for message in body.get("messages", [])
                    if message.get("role") == "user"
                )
            )
        index = min(len(prompts or []) - 1 if prompts is not None else 0, len(responses) - 1)
        if prompts is None:
            index = 0
        return _reply(responses[index])

    if prompts is None:
        # Without a prompt log there is no call counter; keep one of our own.
        calls = {"n": 0}

        def counting_handler(request: httpx.Request) -> httpx.Response:
            index = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return _reply(responses[index])

        return httpx.MockTransport(counting_handler)
    return httpx.MockTransport(handler)


def _good_payload(
    *,
    message: str = "Plan updated.",
    write_scope: list[str] | None = None,
) -> dict[str, Any]:
    scope = ["src/parser.py"] if write_scope is None else write_scope
    return {
        "assistant_message": message,
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Implement parser retry loop",
                    "description": "Add a bounded retry loop to the parser module.",
                    "acceptance_criteria": ["Retries are bounded."],
                    "dependencies": [],
                    "estimated_files": list(scope),
                    "write_scope": list(scope),
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_policy_defaults_to_three_attempts_and_two_clarification_rounds() -> None:
    policy = resolve_plan_repair_policy(None)

    assert policy.enabled is True
    assert policy.max_payload_attempts == 3
    assert policy.max_clarification_rounds == 2
    assert policy.max_repair_retries == 2


def test_policy_attempts_are_configurable_and_capped(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_PLAN_REPAIR_ATTEMPTS", "5")
    monkeypatch.setenv("ALYSIS_PLAN_REPAIR_CLARIFICATION_ROUNDS", "1")
    assert resolve_plan_repair_policy(None).max_payload_attempts == 5
    assert resolve_plan_repair_policy(None).max_clarification_rounds == 1

    monkeypatch.setenv("ALYSIS_PLAN_REPAIR_ATTEMPTS", "9999")
    assert resolve_plan_repair_policy(None).max_payload_attempts == 6

    # A nonsense value falls back to the default rather than disabling the bound.
    monkeypatch.setenv("ALYSIS_PLAN_REPAIR_ATTEMPTS", "not-a-number")
    assert resolve_plan_repair_policy(None).max_payload_attempts == 3


def test_kill_switch_restores_the_single_retry_shape(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_PLAN_REPAIR", "off")
    policy = resolve_plan_repair_policy(None)

    assert policy.enabled is False
    assert policy.max_payload_attempts == 2
    assert policy.max_clarification_rounds == 0


def test_strictness_rises_and_reserves_the_final_rung_for_the_last_attempt() -> None:
    first = repair_strictness_instruction(attempt=2, max_attempts=4)
    second = repair_strictness_instruction(attempt=3, max_attempts=4)
    last = repair_strictness_instruction(attempt=4, max_attempts=4)

    assert first != second != last
    assert "STRICT MODE" in second
    assert "FINAL ATTEMPT" in last
    # The final rung is reserved: only the last allowed attempt gets it.
    assert "FINAL ATTEMPT" not in first
    assert "FINAL ATTEMPT" not in second
    # With the default cap the last follow-up still gets the final wording.
    assert "FINAL ATTEMPT" in repair_strictness_instruction(attempt=3, max_attempts=3)


def test_host_repaired_field_paths_names_dropped_and_coerced_fields() -> None:
    raw = {
        "assistant_message": "",
        "unknown_top_level": True,
        "plan_update": {"tasks_add": [{"title": "a", "description": "b", "nonsense": 1}]},
    }
    repaired = {
        "assistant_message": "Planner update ready.",
        "plan_update": {"tasks_add": [{"title": "a", "description": "b"}]},
    }

    paths = host_repaired_field_paths(raw=raw, repaired=repaired)

    assert "assistant_message" in paths
    assert "unknown_top_level" in paths
    assert "plan_update.tasks_add[0].nonsense" in paths
    # Fields the repairer left alone are not reported.
    assert "plan_update.tasks_add[0].title" not in paths


def test_plan_update_proposes_task_work_ignores_clarifying_turns() -> None:
    assert plan_update_proposes_task_work({"tasks_add": [{"title": "x"}]}) is True
    assert plan_update_proposes_task_work({"tasks_update": [{"id": "T01"}]}) is True
    assert plan_update_proposes_task_work({"project_goal": "new goal"}) is False
    assert plan_update_proposes_task_work({"tasks_add": []}) is False
    assert plan_update_proposes_task_work(None) is False


# ---------------------------------------------------------------------------
# Draft vs execution_ready status
# ---------------------------------------------------------------------------


def test_plan_with_unrunnable_scope_is_saved_as_draft() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Implement parser retry loop",
            "description": "Add a bounded retry loop to the parser module.",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "status": "planned",
        }
    ]

    assessment = apply_plan_status(
        plan, validation_warnings=["Task T01 is missing acceptance_criteria"]
    )

    assert assessment.status == PLAN_STATUS_DRAFT
    assert any("R4" in reason for reason in assessment.blocking_reasons)
    assert plan["plan_status"] == PLAN_STATUS_DRAFT
    assert plan["plan_status_detail"]["warnings"] == ["Task T01 is missing acceptance_criteria"]
    assert plan_status(plan) == PLAN_STATUS_DRAFT


def test_warning_only_plan_is_execution_ready_not_draft() -> None:
    """A missing acceptance_criteria is advisory; the exec gate lets it through."""
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Implement parser retry loop",
            "description": "Add a bounded retry loop to the parser module.",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": ["src/parser.py"],
            "write_scope": ["src/parser.py"],
            "status": "planned",
        }
    ]
    warnings = ["Task T01 is missing acceptance_criteria"]

    assessment = apply_plan_status(plan, validation_warnings=warnings)

    # Status mirrors the execution gate exactly, so the two can never disagree.
    assert assessment.status == PLAN_STATUS_EXECUTION_READY
    assert assessment.blocking_reasons == []
    # The warning is still recorded, just not treated as blocking.
    assert assessment.warnings == warnings


def test_plan_status_falls_back_to_a_live_assessment_for_plans_saved_before_the_field() -> None:
    legacy = _base_plan()
    assert "plan_status" not in legacy

    assert plan_status(legacy) == PLAN_STATUS_DRAFT
    assert assess_plan_status(legacy).blocking_reasons


# ---------------------------------------------------------------------------
# Clarification loop tracking
# ---------------------------------------------------------------------------


def test_clarification_tracker_counts_streak_and_resets_on_plan_update() -> None:
    tracker = ClarificationLoopTracker(max_rounds=2)

    assert tracker.record(goal_key="goal-a", awaiting_clarification=True) == 1
    assert tracker.cap_reached("goal-a") is False
    assert tracker.record(goal_key="goal-a", awaiting_clarification=True) == 2
    assert tracker.cap_reached("goal-a") is True

    # A turn that produced a plan update ends the streak.
    assert tracker.record(goal_key="goal-a", awaiting_clarification=False) == 0
    assert tracker.cap_reached("goal-a") is False


def test_clarification_tracker_restarts_when_the_goal_changes() -> None:
    tracker = ClarificationLoopTracker(max_rounds=2)
    tracker.record(goal_key="goal-a", awaiting_clarification=True)
    tracker.record(goal_key="goal-a", awaiting_clarification=True)
    assert tracker.cap_reached("goal-a") is True

    assert tracker.rounds_for("goal-b") == 0
    assert tracker.cap_reached("goal-b") is False
    assert tracker.record(goal_key="goal-b", awaiting_clarification=True) == 1


def test_clarification_goal_key_prefers_the_plan_goal() -> None:
    plan = _base_plan()
    assert clarification_goal_key(plan=plan, user_text="anything") == (
        "harden the planner payload path"
    )
    # Before a goal exists the message itself is the only anchor.
    assert clarification_goal_key(plan={}, user_text="  Build   a CLI  ") == "build a cli"


# ---------------------------------------------------------------------------
# Repair metadata on the plan
# ---------------------------------------------------------------------------


def test_record_plan_repair_only_records_degraded_turns() -> None:
    plan = _base_plan()

    record_plan_repair(plan, PlannerRepairReport(terminal_state=TERMINAL_VALIDATED))
    assert "plan_repair" not in plan

    # A payload that validated but never became execution-ready is still history
    # worth keeping: it explains a plan that is not what the planner was asked for.
    unready = _base_plan()
    record_plan_repair(
        unready,
        PlannerRepairReport(
            terminal_state=TERMINAL_VALIDATED,
            execution_readiness_errors=["R4 task=T01 observed=missing field: write_scope"],
        ),
    )
    assert len(plan_repair_metadata(unready)["entries"]) == 1
    assert plan_repair_metadata(unready)["host_repaired"] is False

    record_plan_repair(
        plan,
        PlannerRepairReport(
            terminal_state=TERMINAL_HOST_REPAIRED,
            host_repaired=True,
            host_repaired_fields=["plan_update.tasks_add[0].write_scope"],
        ),
    )

    metadata = plan_repair_metadata(plan)
    assert metadata["host_repaired"] is True
    assert metadata["host_repaired_fields"] == ["plan_update.tasks_add[0].write_scope"]
    assert len(metadata["entries"]) == 1


def test_repair_event_payload_carries_status_and_repaired_fields() -> None:
    plan = _base_plan()
    record_plan_repair(
        plan,
        PlannerRepairReport(
            terminal_state=TERMINAL_FORCED_DRAFT,
            forced_draft=True,
            host_repaired=True,
            host_repaired_fields=["assistant_message"],
        ),
    )
    apply_plan_status(plan)

    payload = plan_repair_event_payload(plan)

    assert payload["plan_status"] == PLAN_STATUS_DRAFT
    assert payload["host_repaired"] is True
    assert payload["host_repaired_fields"] == ["assistant_message"]
    assert payload["forced_draft"] is True
    assert payload["plan_status_blocking_reasons"]


def test_recorded_repair_history_stays_bounded() -> None:
    plan = _base_plan()
    for index in range(40):
        record_plan_repair(
            plan,
            PlannerRepairReport(
                terminal_state=TERMINAL_HOST_REPAIRED,
                host_repaired=True,
                host_repaired_fields=[f"field_{index}"],
            ),
        )

    assert len(plan_repair_metadata(plan)["entries"]) == 20


# ---------------------------------------------------------------------------
# Malformed model output through run_planner_turn
# ---------------------------------------------------------------------------


def test_wrong_types_are_repaired_by_re_prompting_the_model(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    # acceptance_criteria as a bare string, dependencies as a number: both are
    # type errors the strict validator rejects outright.
    broken = json.dumps(
        {
            "assistant_message": "Plan updated.",
            "questions": [],
            "plan_update": {
                "tasks_add": [
                    {
                        "title": "Implement parser retry loop",
                        "description": "Add a bounded retry loop.",
                        "acceptance_criteria": "Retries are bounded.",
                        "dependencies": 3,
                        "estimated_files": ["src/parser.py"],
                        "write_scope": ["src/parser.py"],
                    }
                ]
            },
        }
    )
    transport = _scripted_transport([broken, json.dumps(_good_payload())], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    assert result.error is None
    assert result.plan_update is not None
    # The model fixed it, so nothing was salvaged host-side.
    assert result.repair.terminal_state == TERMINAL_VALIDATED
    assert result.repair.host_repaired is False
    assert result.repair.schema_retries == 1
    assert result.repair.attempts == 2
    # The retry names the exact validation error and the attempt number.
    assert len(prompts) == 2
    assert "acceptance_criteria must be an array" in prompts[1]
    assert "Correction attempt 2 of 3" in prompts[1]


def test_unknown_keys_exhaust_retries_then_host_repair_records_the_fields(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    # An unsupported key is rejected strictly but is repairable host-side, so
    # this exercises "model first, host last".
    stubborn = json.dumps(
        {
            "assistant_message": "Plan updated.",
            "questions": [],
            "confidence": 0.9,
            "plan_update": {
                "tasks_add": [
                    {
                        "title": "Implement parser retry loop",
                        "description": "Add a bounded retry loop.",
                        "acceptance_criteria": ["Retries are bounded."],
                        "dependencies": [],
                        "estimated_files": ["src/parser.py"],
                        "write_scope": ["src/parser.py"],
                        "owner": "planner",
                    }
                ]
            },
        }
    )
    transport = _scripted_transport([stubborn], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    assert result.error is None
    assert result.plan_update is not None
    assert result.repair.terminal_state == TERMINAL_HOST_REPAIRED
    assert result.repair.host_repaired is True
    # Host repair ran only after the full model budget was spent.
    assert len(prompts) == 3
    assert result.repair.attempts == 3
    assert set(result.repair.host_repaired_fields) == {
        "confidence",
        "plan_update.tasks_add[0].owner",
    }
    # And the reason the host had to step in is recorded alongside the fields.
    host_failures = [
        item
        for item in result.schema_failures
        if item["reason_code"] == "planner_payload_host_repaired"
    ]
    assert len(host_failures) == 1
    assert host_failures[0]["host_repaired_fields"] == result.repair.host_repaired_fields


def test_truncated_json_reaches_a_terminal_state_without_looping(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    truncated = (
        '{"assistant_message": "Plan updated.", "plan_update": {"tasks_add": [{"title": "Imp'
    )
    transport = _scripted_transport([truncated], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    # Truncated JSON is not parseable, so there is nothing for the host to repair.
    assert result.error
    assert result.plan_update is None
    assert result.repair.terminal_state in TERMINAL_STATES
    assert result.repair.terminal_state == TERMINAL_FAILED
    # Bounded: exactly the configured attempt budget, no more.
    assert len(prompts) == 3
    assert [item["attempt"] for item in result.schema_failures] == [1, 2, 3]
    assert "not valid JSON" in result.schema_failures[0]["error"]


def test_attempt_budget_is_configurable_end_to_end(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    monkeypatch.setenv("ALYSIS_PLAN_REPAIR_ATTEMPTS", "2")
    prompts: list[str] = []
    transport = _scripted_transport(["nonsense"], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    assert len(prompts) == 2
    assert result.repair.max_attempts == 2
    assert result.repair.terminal_state == TERMINAL_FAILED


def test_repair_retries_use_the_json_retry_temperature(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    temperatures: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        temperatures.append(float(body.get("temperature")))
        if len(temperatures) == 1:
            return _reply("not-json")
        return _reply(json.dumps(_good_payload()))

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert temperatures == [0.2, 0.5]


def test_execution_unready_plan_update_is_re_prompted_with_the_rule_errors(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    # Schema-valid, but the task has no runnable write scope: exec would reject it.
    unready = json.dumps(_good_payload(write_scope=[]))
    transport = _scripted_transport([unready, json.dumps(_good_payload())], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    assert result.error is None
    assert result.repair.terminal_state == TERMINAL_VALIDATED
    assert result.repair.readiness_retries == 1
    assert result.repair.schema_retries == 0
    assert len(prompts) == 2
    # The re-prompt states what actually went wrong -- the task was thrown away
    # for lacking runnable scope -- not a bogus schema complaint.
    assert "Execution-readiness repair follow-up" in prompts[1]
    assert "DROPPED 1 of 1 proposed tasks_add task" in prompts[1]
    assert "requires runnable estimated_files/write_scope" in prompts[1]
    assert "did not match the required schema" not in prompts[1]
    assert result.plan_update["tasks_add"][0]["write_scope"] == ["src/parser.py"]


def test_landed_task_failing_an_acceptance_rule_is_re_prompted_with_the_rule_id(
    monkeypatch,
) -> None:
    """A task that survives apply but fails R1-R5 is reported by rule id."""
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    # Docs-only scope survives apply (it is runnable file scope) but fails R3:
    # there is no primary implementation path for a mutating task.
    docs_only = json.dumps(
        {
            "assistant_message": "Plan updated.",
            "questions": [],
            "plan_update": {
                "tasks_add": [
                    {
                        "title": "Implement parser retry loop",
                        "description": "Add a bounded retry loop to the parser module.",
                        "acceptance_criteria": ["Retries are bounded."],
                        "dependencies": [],
                        "estimated_files": ["docs/parser.md"],
                        "write_scope": ["docs/parser.md"],
                    }
                ]
            },
        }
    )
    transport = _scripted_transport([docs_only, json.dumps(_good_payload())], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    assert result.error is None
    assert result.repair.readiness_retries == 1
    assert len(prompts) == 2
    assert "Execution-readiness repair follow-up" in prompts[1]
    assert "R3" in prompts[1]
    assert "DROPPED" not in prompts[1]


def test_task_update_for_an_unknown_id_is_re_prompted(monkeypatch) -> None:
    """An update aimed at a task that does not exist can never land."""
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    ghost_update = json.dumps(
        {
            "assistant_message": "Plan updated.",
            "questions": [],
            "plan_update": {
                "tasks_update": [{"id": "T99", "write_scope": ["src/parser.py"]}],
            },
        }
    )
    transport = _scripted_transport([ghost_update, json.dumps(_good_payload())], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    assert result.error is None
    assert result.repair.readiness_retries == 1
    assert len(prompts) == 2
    assert "tasks_update entries for task ids that do not exist" in prompts[1]
    assert "T99" in prompts[1]


def test_persistently_unready_plan_update_is_kept_as_a_draft(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    unready = json.dumps(_good_payload(write_scope=[]))
    transport = _scripted_transport([unready], prompts=prompts)

    plan = _base_plan()
    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=plan,
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    # A payload that validated is real work: it is kept, not thrown away, and the
    # plan it produces is recorded as a draft instead of failing later at exec.
    assert result.error is None
    assert result.plan_update is not None
    assert len(prompts) == 3
    assert result.repair.readiness_retries == 2
    assert result.repair.execution_readiness_errors

    from alysis_code.plan_assistant import apply_guarded_planner_plan_update

    apply_guarded_planner_plan_update(plan, copy.deepcopy(result.plan_update))
    assert apply_plan_status(plan).status == PLAN_STATUS_DRAFT


def test_pre_existing_unreadiness_does_not_trigger_repair_retries(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    plan = _base_plan()
    # The plan is already unready before this turn starts.
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Implement legacy parser change",
            "description": "Change the legacy parser.",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "status": "planned",
        }
    ]
    transport = _scripted_transport([json.dumps(_good_payload())], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=plan,
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    # Inherited failures are not this turn's fault; re-prompting about them would
    # burn the budget without any way for the planner to make progress.
    assert result.error is None
    assert len(prompts) == 1
    assert result.repair.terminal_state == TERMINAL_VALIDATED
    assert result.repair.readiness_retries == 0


def test_clarification_only_turn_is_not_held_to_the_acceptance_rules(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    prompts: list[str] = []
    clarifying = json.dumps(
        {
            "assistant_message": "A couple of questions first.",
            "questions": ["Which module owns the parser?"],
            "plan_update": None,
        }
    )
    transport = _scripted_transport([clarifying], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Can you help me with something?",
        transport=transport,
    )

    assert result.error is None
    assert len(prompts) == 1
    assert result.repair.terminal_state == TERMINAL_VALIDATED
    assert result.repair.readiness_retries == 0


def test_clarification_cap_forces_a_concrete_draft_instead_of_stalling(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    clarifying = json.dumps(
        {
            "assistant_message": "I need more detail before planning.",
            "questions": ["What exactly should change?"],
            "plan_update": None,
        }
    )
    transport = _scripted_transport([clarifying])

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Update the retry handling in the parser module",
        workspace_context={
            "workspace_kind": "git_repo",
            "greenfield": False,
            "observed_paths": ["src/parser.py"],
            "top_level_entries": [{"path": "src", "kind": "dir"}],
        },
        # Two clarification rounds already spent on this same goal.
        clarification_rounds=2,
        transport=transport,
    )

    assert result.error is None
    assert result.repair.terminal_state == TERMINAL_FORCED_DRAFT
    assert result.repair.forced_draft is True
    assert result.repair.clarification_rounds == 2
    # A real task, and no more questions.
    assert result.questions == []
    assert result.plan_update is not None
    assert result.plan_update["tasks_add"]
    assert "draft" in result.assistant_message.casefold()


def test_below_the_clarification_cap_the_planner_still_gets_to_ask(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    clarifying = json.dumps(
        {
            "assistant_message": "I need more detail before planning.",
            "questions": ["What exactly should change?"],
            "plan_update": None,
        }
    )
    transport = _scripted_transport([clarifying])

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Update the retry handling in the parser module",
        workspace_context={
            "workspace_kind": "git_repo",
            "greenfield": False,
            "observed_paths": ["src/parser.py"],
            "top_level_entries": [{"path": "src", "kind": "dir"}],
        },
        clarification_rounds=1,
        transport=transport,
    )

    assert result.repair.forced_draft is False
    assert result.questions == ["What exactly should change?"]


def test_greenfield_clarification_cap_forces_a_scaffold_draft(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    clarifying = json.dumps(
        {
            "assistant_message": "Tell me more about the app.",
            "questions": ["What language?"],
            "plan_update": None,
        }
    )
    transport = _scripted_transport([clarifying])

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Build me a todo list CLI",
        workspace_context={"workspace_kind": "empty_dir", "greenfield": True},
        clarification_rounds=2,
        transport=transport,
    )

    assert result.repair.terminal_state == TERMINAL_FORCED_DRAFT
    assert result.plan_update["tasks_add"][0]["title"] == "Build requested project scaffold"
    assert result.questions == []


def test_kill_switch_disables_readiness_retries_and_the_clarification_cap(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    monkeypatch.setenv("ALYSIS_PLAN_REPAIR", "off")
    prompts: list[str] = []
    transport = _scripted_transport([json.dumps(_good_payload(write_scope=[]))], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        clarification_rounds=5,
        transport=transport,
    )

    # Legacy behaviour: an execution-unready payload is accepted without retries.
    assert result.error is None
    assert len(prompts) == 1
    assert result.repair.readiness_retries == 0
    assert result.repair.forced_draft is False


def test_kill_switch_restores_eager_host_repair_on_the_first_parse(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    monkeypatch.setenv("ALYSIS_PLAN_REPAIR", "off")
    prompts: list[str] = []
    repairable = json.dumps(
        {
            "assistant_message": "Plan updated.",
            "questions": [],
            "confidence": 0.9,
            "plan_update": _good_payload()["plan_update"],
        }
    )
    transport = _scripted_transport([repairable], prompts=prompts)

    result = run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    # The legacy path repaired host-side on the first parse and never spent a
    # second request on it; reverting the flag has to be a real revert.
    assert result.error is None
    assert result.plan_update is not None
    assert len(prompts) == 1
    assert result.schema_failures == []


def test_every_malformed_shape_reaches_a_terminal_state(monkeypatch) -> None:
    """No input shape leaves the turn without a terminal state or an answer."""
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    shapes = [
        "",  # nothing at all
        "not json at all",
        "[]",  # valid JSON, wrong root type
        "null",
        '{"assistant_message": 42}',  # wrong scalar type
        '{"questions": ["only questions"]}',  # required field missing
        '{"assistant_message": "ok", "plan_update": "a string"}',
        '{"assistant_message": "ok", "plan_update": {"tasks_add": {"title": "x"}}}',
        '{"assistant_message": "ok", "plan_update": {"tasks_add": [{"title": ""}]}}',
        '{"assistant_message": "ok", "plan_update": {"tasks_remove": [7]}}',
        '{"assistant_message": "ok", "plan_update": {"tasks_add": [{"title": "t", "descript',
    ]

    for shape in shapes:
        calls = {"n": 0}

        def handler(
            _request: httpx.Request,
            calls: dict[str, int] = calls,
            content: str = shape,
        ) -> httpx.Response:
            calls["n"] += 1
            return _reply(content)

        result = run_planner_turn(
            cfg=_cfg(),
            api_key_override=None,
            plan=_base_plan(),
            transcript_tail=[],
            user_text="Add a bounded retry loop to the parser",
            transport=httpx.MockTransport(handler),
        )

        assert result.repair.terminal_state in TERMINAL_STATES, shape
        # Either a usable answer or an explicit error -- never both empty.
        assert bool(result.error) or bool(result.assistant_message), shape
        # Bounded in every case, including the empty-response short circuit.
        assert calls["n"] <= 3, shape


def test_malformed_output_never_mutates_the_caller_plan(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    plan = _base_plan()
    before = copy.deepcopy(plan)
    transport = _scripted_transport([json.dumps(_good_payload(write_scope=[]))])

    run_planner_turn(
        cfg=_cfg(),
        api_key_override=None,
        plan=plan,
        transcript_tail=[],
        user_text="Add a bounded retry loop to the parser",
        transport=transport,
    )

    # The readiness preview simulates the update on a copy; the real plan is
    # untouched until the caller applies the result itself.
    assert plan == before
