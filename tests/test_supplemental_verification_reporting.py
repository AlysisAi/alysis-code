from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.verification import (
    SUPPLEMENTAL_VERIFICATION_ADVISORY,
    TurnExecutionState,
    _completion_gate_nudge_message,
    _record_tool_effect,
)

_NATIVE_COMMAND = "python3 -m unittest discover -s tests -v"
_CHECK_COMMAND = "python3 checks.py"


def _record(
    root: Path,
    state: TurnExecutionState,
    *,
    tool: str,
    command: str,
    exit_code: int,
    native: bool = False,
) -> dict[str, Any]:
    output = (
        "Ran 2 tests in 0.001s\n\nOK\n"
        if native
        else ("checker passed\n" if exit_code == 0 else "AssertionError: task checker failed\n")
    )
    if tool == "verify_run":
        arguments = {"commands": [command]}
        result: dict[str, Any] = {
            "commands": [command],
            "command_results": [
                {
                    "command": command,
                    "effective_command": command,
                    "ok": exit_code == 0,
                    "exit_code": exit_code,
                    "real_execution": True,
                    "output_preview": output,
                }
            ],
            "all_passed": exit_code == 0,
        }
        if native:
            result["verification_contract_type"] = "repo_native"
            result["verification_command_specs"] = [
                {"original_text": command, "provenance": "PREEXISTING_REPO_NATIVE"}
            ]
    else:
        arguments = {"cmd": command}
        result = {
            "cmd": command,
            "effective_cmd": command,
            "exit_code": exit_code,
            "stdout": output if exit_code == 0 else "",
            "stderr": output if exit_code else "",
        }
    _record_tool_effect(
        root=root,
        state=state,
        tool_name=tool,
        arguments=arguments,
        status="ok",
        result=result,
        known_verification_commands=[_NATIVE_COMMAND],
        verification_authoritative=True,
    )
    return result


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_supplemental_result_reports_its_origin_without_relabeling_prior_checks(
    tmp_path: Path, tool: str, exit_code: int
) -> None:
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={_NATIVE_COMMAND},
    )
    native = _record(
        tmp_path, state, tool="verify_run", command=_NATIVE_COMMAND, exit_code=0, native=True
    )
    assert native["verification_note"] == "Matched command provenance: PREEXISTING_REPO_NATIVE."
    assert state.last_verification_passed is True
    prior_accepted = deepcopy(state.accepted_verification_evidence)
    assert len(prior_accepted) == 1

    # The checker is authored after the existing two-test suite was observed.
    (tmp_path / "checks.py").write_text("assert 1 + 1 == 2\n", encoding="utf-8")
    supplemental = _record(tmp_path, state, tool=tool, command=_CHECK_COMMAND, exit_code=exit_code)

    assert supplemental["verification_evidence_supplemental_only"] is True
    assert supplemental["verification_note"] == (
        "Additional task-check execution; it does not establish coverage "
        "of the resolved verification command."
    )
    assert "verification_supplemental_only_note" not in supplemental
    assert SUPPLEMENTAL_VERIFICATION_ADVISORY not in str(supplemental)
    assert state.accepted_verification_evidence == prior_accepted
    assert state.supplemental_verification_evidence[-1]["observed_exit_code"] == exit_code
    assert state.last_verification_passed is (exit_code == 0)
    if tool == "verify_run":
        assert supplemental["command_results"][0]["exit_code"] == exit_code
        assert supplemental["all_passed"] is (exit_code == 0)
    else:
        assert supplemental["exit_code"] == exit_code


@pytest.mark.parametrize("only_supplemental", [False, True])
def test_aggregate_supplemental_advisory_remains_conditional_at_completion(
    only_supplemental: bool,
) -> None:
    message = _completion_gate_nudge_message(
        ["verification_missing"],
        has_material_edits=True,
        has_only_supplemental_verification_evidence=only_supplemental,
    )

    assert message.count(SUPPLEMENTAL_VERIFICATION_ADVISORY) == int(only_supplemental)
