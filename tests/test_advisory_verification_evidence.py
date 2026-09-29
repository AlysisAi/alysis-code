"""Recommendations do not make other genuine checks insufficient evidence."""

from pathlib import Path

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)
from test_verify_tool import _build_tools, _cp, _patch_host_execution

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.verification import (
    TurnExecutionState,
    _completion_gate_problems,
    _record_tool_effect,
)
from alysis_code.agent.verification_evidence import classify_verification_evidence
from alysis_code.verification_contract import build_verification_command_spec
from alysis_code.verify_gate import ResolvedVerifyCommands, required_verify_commands

RECOMMENDATION = "python -m unittest discover"
REQUESTED = "python3 -m unittest discover -s tests -v"
OUTPUT = "test_case (test_example.Example.test_case) ... ok\nRan 1 test in 0.001s\n\nOK\n"


def _selection(*, required: bool = False, mixed: bool = False) -> ResolvedVerifyCommands:
    commands = (RECOMMENDATION, "pytest -q") if mixed else (RECOMMENDATION,)
    specs = tuple(
        build_verification_command_spec(
            command,
            source="cli.verify_cmd" if required or index else "repo_scan.likely_test_commands",
            contract_type="explicit_override" if required or index else "selected",
        )
        for index, command in enumerate(commands)
    )
    return ResolvedVerifyCommands(
        commands=commands,
        source="cli.verify_cmd" if required else "repo_scan.likely_test_commands",
        contract_type="explicit_override" if required else "selected",
        command_specs=specs,
    )


def _run_tool(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    selection: ResolvedVerifyCommands,
    command: str = REQUESTED,
) -> dict:
    if tool == "verify_run":
        _patch_host_execution(monkeypatch, lambda *_args, **_kwargs: _cp(stdout=OUTPUT))
        arguments = {"commands": [command]}
    else:
        monkeypatch.setattr(
            agent_loop_mod,
            "shell_run",
            lambda **_kwargs: {"cmd": command, "exit_code": 0, "stdout": OUTPUT, "stderr": ""},
        )
        arguments = {"cmd": command}
    tools = _build_tools(
        root,
        effective_verification_commands=list(selection.commands),
        verify_command_selection=selection,
    )
    return tools[tool].run(arguments)


def _record(root, state, tool, result, selection, command=REQUESTED, *, environment_known=True):
    _record_tool_effect(
        root=root,
        state=state,
        tool_name=tool,
        arguments={"commands": [command]} if tool == "verify_run" else {"cmd": command},
        status="ok",
        result=result,
        known_verification_commands=list(selection.commands),
        scope_environment_known=environment_known,
    )


def _gate(state):
    return _completion_gate_problems(
        state=state,
        final_text="Implemented and checked.",
        blocked=False,
        verification_expected=True,
        require_material_edit_evidence=False,
        evidence_v2=True,
        turn_intent="execute",
    )


@pytest.mark.parametrize("tool", ["verify_run", "shell_run"])
def test_advisory_selection_accepts_independent_execution_in_tool_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    selection = _selection()
    result = _run_tool(tmp_path, monkeypatch, tool, selection)
    assert result["verification_evidence_allowed"] is True
    assert result["verification_evidence_supplemental_only"] is False
    assert result["verification_evidence_reason"] == "repo_native_command"
    state = TurnExecutionState(execution_requested=True)
    state.touched_repo_paths.add("src/app.py")
    state.note_material_edit()
    state.note_verification_relevant_edit()
    _record(tmp_path, state, tool, result, selection)
    assert len(state.accepted_verification_evidence) == 1
    assert not state.supplemental_verification_evidence
    assert not state.covered_verification_commands  # No equivalence was invented.
    assert "resolved verification command" not in result.get("verification_note", "")
    assert _gate(state) == []
    state.note_material_edit()
    state.note_verification_relevant_edit()
    assert "verification_incomplete" in _gate(state)  # Freshness remains independent.


@pytest.mark.parametrize("tool", ["verify_run", "shell_run"])
def test_explicit_selection_still_requires_exact_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    selection = _selection(required=True)
    result = _run_tool(tmp_path, monkeypatch, tool, selection)
    assert result["verification_evidence_allowed"] is False
    assert result["verification_evidence_supplemental_only"] is True
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands=set(required_verify_commands(selection)),
    )
    _record(tmp_path, state, tool, result, selection)
    assert state.missing_verification_commands() == {RECOMMENDATION}
    assert not state.accepted_verification_evidence
    assert "verification_incomplete" in _gate(state)


@pytest.mark.parametrize("command", [REQUESTED, RECOMMENDATION])
def test_mixed_selection_does_not_credit_required_check_from_another_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    selection = _selection(mixed=True)
    result = _run_tool(tmp_path, monkeypatch, "verify_run", selection, command)
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands=set(required_verify_commands(selection)),
    )
    _record(tmp_path, state, "verify_run", result, selection, command)
    assert state.missing_verification_commands() == {"pytest -q"}
    assert "verification_incomplete" in _gate(state)
    assert "pytest -q" not in state.covered_verification_commands
    assert result["verification_evidence_supplemental_only"] is (command == REQUESTED)


def test_legacy_classifier_without_requirement_information_stays_conservative() -> None:
    evidence = classify_verification_evidence(
        REQUESTED,
        known_verification_commands=[RECOMMENDATION],
        exit_code=0,
        output=OUTPUT,
        real_execution=True,
    )
    assert evidence.supplemental_only
    assert not evidence.allowed_to_satisfy_contract


@pytest.mark.parametrize("known", [None, []])
@pytest.mark.parametrize("tool", ["verify_run", "shell_run"])
def test_required_command_is_matchable_without_a_separate_known_list(
    tmp_path: Path, known: list[str] | None, tool: str
) -> None:
    evidence = classify_verification_evidence(
        REQUESTED,
        known_verification_commands=known,
        required_verification_commands={REQUESTED},
        exit_code=0,
        output=OUTPUT,
        real_execution=True,
    )
    assert evidence.covered_verification_commands == (REQUESTED,)
    assert evidence.allowed_to_satisfy_contract
    result = (
        {
            "commands": [REQUESTED],
            "all_passed": True,
            "status": "passed",
            "command_results": [
                {
                    "command": REQUESTED,
                    "status": "passed",
                    "exit_code": 0,
                    "real_execution": True,
                    "output_preview": OUTPUT,
                }
            ],
        }
        if tool == "verify_run"
        else {"cmd": REQUESTED, "exit_code": 0, "stdout": OUTPUT}
    )
    state = TurnExecutionState(execution_requested=True, expected_verification_commands={REQUESTED})
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name=tool,
        arguments={},
        status="ok",
        result=result,
        known_verification_commands=known,
    )
    assert state.covered_verification_commands == {REQUESTED}
    assert not state.missing_verification_commands()
    assert _gate(state) == []


@pytest.mark.parametrize(
    ("stage_status", "allowed", "reason"),
    [
        (None, False, "pipeline_stage_status_unavailable"),
        ([0, 0], True, "repo_native_command"),
        ([1, 0], False, "non_executing_or_failed_repo_native_command"),
    ],
)
def test_advisory_pipeline_still_requires_observed_first_stage(
    stage_status, allowed, reason
) -> None:
    evidence = classify_verification_evidence(
        REQUESTED + " | tail -5",
        known_verification_commands=[RECOMMENDATION],
        required_verification_commands=(),
        stage_status=stage_status,
        output=OUTPUT,
    )
    assert evidence.allowed_to_satisfy_contract is allowed
    assert evidence.reason == reason
    assert not evidence.covered_verification_commands


@pytest.mark.parametrize(
    "observation",
    [
        {"exit_code": 1, "real_execution": True},
        {"exit_code": 127, "real_execution": False},
        {"exit_code": None, "real_execution": None, "output": ""},
        {"exit_code": 0, "real_execution": True, "material_touched_paths": {"app.py"}},
    ],
)
def test_advisory_independent_check_keeps_execution_and_mutation_safeguards(observation) -> None:
    arguments = {"exit_code": 0, "real_execution": True, "output": OUTPUT, **observation}
    evidence = classify_verification_evidence(
        REQUESTED,
        known_verification_commands=[RECOMMENDATION],
        required_verification_commands=(),
        **arguments,
    )
    assert not evidence.allowed_to_satisfy_contract
    assert not evidence.covered_verification_commands


def test_explicit_check_from_different_working_directory_does_not_cover_requirement(
    tmp_path: Path,
) -> None:
    evidence = classify_verification_evidence(
        RECOMMENDATION,
        known_verification_commands=[RECOMMENDATION],
        required_verification_commands={RECOMMENDATION},
        working_directory="other",
        root=tmp_path,
        exit_code=0,
        output=OUTPUT,
        real_execution=True,
    )
    assert evidence.supplemental_only
    assert not evidence.allowed_to_satisfy_contract
    assert not evidence.covered_verification_commands


@pytest.mark.parametrize("real_execution", [True, False, None])
@pytest.mark.parametrize("environment_known", [True, False])
def test_independent_pass_does_not_clear_different_failed_advisory_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_execution: bool | None,
    environment_known: bool,
) -> None:
    selection = _selection()
    state = TurnExecutionState(execution_requested=True)
    failed = {
        "commands": [RECOMMENDATION],
        "command_results": [
            {
                "command": RECOMMENDATION,
                "exit_code": 127,
                "status": "failed",
                "real_execution": real_execution,
                "output_preview": "Unable to complete this check.",
            }
        ],
        "status": "failed",
        "all_passed": False,
    }
    _record(
        tmp_path,
        state,
        "verify_run",
        failed,
        selection,
        RECOMMENDATION,
        environment_known=environment_known,
    )
    assert state.last_verification_passed is False
    passed = _run_tool(tmp_path, monkeypatch, "verify_run", selection)
    _record(tmp_path, state, "verify_run", passed, selection)
    assert state.last_verification_passed is False
    assert state.observed_verification_failures
    assert "verification_failed" in _gate(state)
