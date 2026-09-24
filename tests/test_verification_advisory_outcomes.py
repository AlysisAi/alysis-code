"""Advisory selections must not hide observed failures or become requirements."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.verification import (
    TurnExecutionState,
    _completion_gate_problems,
    _record_tool_effect,
)

_RECOMMENDED = "python -m unittest discover"
_ALTERNATE = "python3 -m unittest discover -s tests -v"


def _observe(
    root: Path,
    state: TurnExecutionState,
    command: str,
    *,
    exit_code: int = 0,
    real_execution: bool | None = True,
    command_status: str = "passed",
    output: str = "Ran 1 test in 0.001s\n\nOK\n",
    environment_known: bool = True,
) -> dict[str, Any]:
    item = {
        "command": command,
        "effective_command": command,
        "exit_code": exit_code,
        "real_execution": real_execution,
        "status": command_status,
        "ok": exit_code == 0 and real_execution is True,
        "output_preview": output,
    }
    result = {
        "commands": [command],
        "command_results": [item],
        "all_passed": item["ok"],
    }
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="verify_run",
        arguments={"commands": [command]},
        status="ok",
        result=result,
        known_verification_commands=[_RECOMMENDED],
        scope_environment_known=environment_known,
    )
    return result


def _problems(state: TurnExecutionState) -> list[str]:
    return _completion_gate_problems(
        state=state,
        final_text="Checked the implementation.",
        blocked=False,
        verification_expected=True,
        require_material_edit_evidence=False,
        evidence_v2=True,
        turn_intent="execute",
    )


@pytest.mark.parametrize("real_execution", [True, None])
@pytest.mark.parametrize("environment_known", [True, False])
def test_different_pass_cannot_erase_failed_advisory_selection(
    tmp_path, real_execution, environment_known
):
    state = TurnExecutionState(execution_requested=True)
    _observe(
        tmp_path,
        state,
        _RECOMMENDED,
        exit_code=1,
        real_execution=real_execution,
        command_status="failed",
        output="Ran 1 test in 0.001s\n\nFAILED (failures=1)\n",
        environment_known=environment_known,
    )
    assert len(state.observed_verification_failures) == 1
    failed = dict(state.observed_verification_failures)
    _observe(tmp_path, state, _ALTERNATE, environment_known=environment_known)
    assert state.observed_verification_failures == failed
    assert state.last_verification_passed is False
    assert state.expected_verification_commands == set()
    assert "verification_failed" in _problems(state)


def test_actual_advisory_rerun_resolves_only_its_own_failure(tmp_path):
    state = TurnExecutionState(execution_requested=True)
    _observe(
        tmp_path,
        state,
        _RECOMMENDED,
        exit_code=1,
        command_status="failed",
        output="Ran 1 test in 0.001s\n\nFAILED (failures=1)\n",
    )
    assert len(state.observed_verification_failures) == 1
    _observe(tmp_path, state, _RECOMMENDED)
    assert not state.observed_verification_failures
    assert state.last_verification_passed is True
    assert state.expected_verification_commands == set()
    assert _problems(state) == []


def test_independent_pass_supersedes_nonexecuting_advisory_attempt(
    tmp_path,
):
    state = TurnExecutionState(execution_requested=True)
    _observe(
        tmp_path,
        state,
        _RECOMMENDED,
        real_execution=False,
        command_status="not_executed",
        output="No verification work was executed.\n",
    )
    assert state.last_verification_passed is False
    assert not state.observed_verification_failures
    result = _observe(tmp_path, state, _ALTERNATE)
    assert result["verification_evidence_supplemental_only"] is False
    assert state.last_verification_passed is True
    assert state.expected_verification_commands == set()
    assert state.covered_verification_commands == set()
    assert len(state.accepted_verification_evidence) == 1
    assert state.accepted_verification_evidence[0]["normalized_command"] == _ALTERNATE
    assert _problems(state) == []


def test_required_nonexecution_stays_missing_after_different_pass(tmp_path):
    state = TurnExecutionState(
        execution_requested=True, expected_verification_commands={_RECOMMENDED}
    )
    _observe(
        tmp_path,
        state,
        _RECOMMENDED,
        exit_code=127,
        real_execution=False,
        command_status="failed",
        output="The configured verifier could not be launched.\n",
    )
    result = _observe(tmp_path, state, _ALTERNATE)
    assert result["verification_evidence_supplemental_only"] is True
    assert state.missing_verification_commands() == {_RECOMMENDED}
    assert state.failed_verification_commands() == {_RECOMMENDED}
    assert state.covered_verification_commands == set()
    assert not state.accepted_verification_evidence
    assert "verification_failed" in _problems(state)


@pytest.mark.parametrize("real_execution", [False, None, True])
@pytest.mark.parametrize("exit_code", [126, 127])
def test_nonzero_status_is_ambiguous_even_when_legacy_assessment_says_not_executed(
    tmp_path, real_execution, exit_code
):
    state = TurnExecutionState(execution_requested=True)
    _observe(
        tmp_path,
        state,
        _RECOMMENDED,
        exit_code=exit_code,
        real_execution=real_execution,
        command_status="failed",
        output="Verifier returned a failure.\n",
    )
    assert len(state.observed_verification_failures) == 1
    _observe(tmp_path, state, _ALTERNATE)
    assert state.last_verification_passed is False
    assert "verification_failed" in _problems(state)
