from __future__ import annotations

from alysis_code.agent.turn_contract import TurnOutcome, TurnSemantics
from alysis_code.agent_loop import (
    _SYSTEM_PROMPT_ONE_SHOT_SECTION,
    SYSTEM_PROMPT,
    _completion_gate_nudge_message,
)


def test_one_shot_prompt_forbids_standalone_text_only_plan() -> None:
    assert "Do not emit a standalone text-only plan and wait for the user." in (
        _SYSTEM_PROMPT_ONE_SHOT_SECTION
    )
    assert "Planning may be internal" in _SYSTEM_PROMPT_ONE_SHOT_SECTION


def test_one_shot_visible_plan_must_be_accompanied_by_tool_calls() -> None:
    assert "same assistant response must also include implementation-oriented tool calls." in (
        _SYSTEM_PROMPT_ONE_SHOT_SECTION
    )


def test_one_shot_read_or_explore_only_progress_is_not_final() -> None:
    assert "A progress update is not a final answer." in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    assert "After read/explore-only tool calls" in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    assert "run an implementation-producing command" in _SYSTEM_PROMPT_ONE_SHOT_SECTION


def test_one_shot_prompt_rejects_generic_clarification_bailouts() -> None:
    assert "Do not ask a generic clarification question" in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    assert "safe best effort" in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    assert "destructive alternatives require the user's choice" in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    assert "proceed safely or call report_blocker" in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    assert "never ask a question and wait" in _SYSTEM_PROMPT_ONE_SHOT_SECTION


def test_one_shot_prompt_mentions_requirement_review_and_root_fixing() -> None:
    prompt = _SYSTEM_PROMPT_ONE_SHOT_SECTION.casefold()

    assert "re-read" in prompt and "requirement" in prompt
    assert "definition" in prompt and "direct call" in prompt


def test_one_shot_prompt_protects_existing_tests_and_requires_execution_evidence() -> None:
    assert "tracked existing tests as immutable acceptance evidence" in (
        _SYSTEM_PROMPT_ONE_SHOT_SECTION
    )
    assert "New test files are allowed" in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    # The execution-evidence rule lives once, in the base prompt's final
    # response requirements, and applies to one-shot runs through composition.
    assert "after your last source edit" in SYSTEM_PROMPT
    assert "observing its output and exit code" in SYSTEM_PROMPT


def test_no_material_edits_nudge_is_implementation_first() -> None:
    message = _completion_gate_nudge_message(["no_material_edits", "verification_not_attempted"])

    assert "No file changes are recorded yet" in message
    assert "Expected verification has not been completed" in message
    assert message.index("No file changes are recorded yet") < message.index(
        "Expected verification has not been completed"
    )
    assert "this checklist is advisory" in message


def test_verification_not_attempted_nudge_is_verification_first() -> None:
    message = _completion_gate_nudge_message(["verification_not_attempted"])

    assert "Expected verification has not been completed" in message
    assert "No file changes are recorded yet" not in message
    assert "this checklist is advisory" in message


def test_semantic_outcomes_drive_execution_posture() -> None:
    assert TurnSemantics(outcome=TurnOutcome.PLAN).execution_posture == "plan_or_analysis_only"
    assert TurnSemantics(outcome=TurnOutcome.INSPECT).execution_posture == "advisory_non_execution"
    assert TurnSemantics(outcome=TurnOutcome.CHANGE).execution_posture == "execute"
