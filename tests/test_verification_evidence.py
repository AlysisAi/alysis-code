from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.agent.acceptance_contract import (
    EvidenceOrigin,
    build_acceptance_contract,
    classify_evidence_origin,
)
from alysis_code.agent_loop import (
    TurnExecutionState,
    VerificationEvidenceCategory,
    _fresh_executed_evidence_for_claim,
    _shell_command_is_verification_attempt,
    _successful_verification_claim_kind,
    classify_verification_evidence,
)


def test_authoritative_matching_command_is_authoritative_evidence() -> None:
    evidence = classify_verification_evidence(
        "PYTHONPATH=src pytest tests/test_cli.py -q",
        known_verification_commands=["pytest -q"],
        authoritative=True,
        exit_code=0,
        output="1 passed\n",
        real_execution=True,
    )

    assert evidence.category == VerificationEvidenceCategory.AUTHORITATIVE
    assert evidence.allowed_to_satisfy_contract is True
    assert evidence.covered_verification_commands == ("pytest -q",)
    assert evidence.reason == "matched_authoritative_contract"


def test_authoritative_vacuous_contract_command_is_not_evidence() -> None:
    evidence = classify_verification_evidence(
        "true",
        known_verification_commands=["true"],
        authoritative=True,
        exit_code=0,
        output="",
    )

    assert evidence.category == VerificationEvidenceCategory.AUTHORITATIVE
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.covered_verification_commands == ("true",)
    assert evidence.reason == "vacuous_verifier"


def test_piped_non_verifier_first_stage_is_not_evidence() -> None:
    # Evidence v2 judges the pipeline's first stage from observed facts: a
    # non-verifier first stage does not count, even piped through tail.
    command = "tool args | tail -n 1"

    evidence = classify_verification_evidence(
        command,
        known_verification_commands=[command],
        authoritative=False,
        output="ok\n",
        stage_status=[0, 0],
    )

    assert evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.covered_verification_commands == ()


def test_legacy_classifier_rejects_pipeline_by_string_shape() -> None:
    # With the kill-switch off, the legacy string-shape classifier rejects any
    # pipeline as unsafe regardless of observed facts.
    command = "tool args | tail -n 1"

    evidence = classify_verification_evidence(
        command,
        known_verification_commands=[command],
        authoritative=False,
        exit_code=0,
        output="ok\n",
        real_execution=True,
        evidence_v2=False,
    )

    assert evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION
    assert evidence.reason == "unsafe_pipeline"


def test_trusted_command_that_mutates_source_is_supplemental_until_clean_rerun() -> None:
    command = "python check.py"

    mutating = classify_verification_evidence(
        command,
        known_verification_commands=[command],
        exit_code=0,
        output="ok\n",
        real_execution=True,
        material_touched_paths={"src/app.py"},
    )
    clean = classify_verification_evidence(
        command,
        known_verification_commands=[command],
        exit_code=0,
        output="ok\n",
        real_execution=True,
        material_touched_paths=set(),
    )

    assert mutating.allowed_to_satisfy_contract is False
    assert mutating.reason == "mutated_material_paths"
    assert clean.allowed_to_satisfy_contract is True
    assert clean.covered_verification_commands == (command,)


def test_curl_fail_probe_can_satisfy_explicit_contract() -> None:
    command = "curl -fsS http://127.0.0.1:3000/health"

    evidence = classify_verification_evidence(
        command,
        known_verification_commands=[command],
        authoritative=True,
        exit_code=0,
        output="ok\n",
        real_execution=True,
    )

    assert evidence.category == VerificationEvidenceCategory.AUTHORITATIVE
    assert evidence.allowed_to_satisfy_contract is True
    assert evidence.reason == "matched_authoritative_contract"


def test_plain_curl_probe_is_inconclusive_contract_evidence() -> None:
    command = "curl -s http://127.0.0.1:3000/health"

    evidence = classify_verification_evidence(
        command,
        known_verification_commands=[command],
        authoritative=True,
        exit_code=0,
        output="HTTP 500\n",
        real_execution=None,
    )

    assert evidence.category == VerificationEvidenceCategory.AUTHORITATIVE
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.reason == "http_probe_requires_curl_fail"


def test_authoritative_contract_rejects_unrelated_task_specific_check(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")

    evidence = classify_verification_evidence(
        "python check.py",
        known_verification_commands=["pytest -q"],
        authoritative=True,
        changed_paths={"check.py"},
        exit_code=0,
        output="ok\n",
        root=tmp_path,
    )

    assert evidence.category == VerificationEvidenceCategory.TASK_ACCEPTANCE
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.supplemental_only is True
    assert evidence.covered_verification_commands == ()


def test_partial_authoritative_coverage_remains_partial() -> None:
    evidence = classify_verification_evidence(
        "pytest tests/test_cli.py -q",
        known_verification_commands=["pytest -q", "ruff check ."],
        authoritative=True,
        exit_code=0,
        output="1 passed\n",
        real_execution=True,
    )

    assert evidence.category == VerificationEvidenceCategory.AUTHORITATIVE
    assert evidence.allowed_to_satisfy_contract is True
    assert evidence.covered_verification_commands == ("pytest -q",)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "python -m pytest tests/test_cli.py -q",
        "python -m unittest -v",
        "go test ./...",
        "cargo test",
        "npm test",
        "pnpm test",
        "yarn test",
        "make verify",
        "just check",
        "ruff check src",
    ],
)
def test_repo_native_commands_are_classified_without_known_contract(command: str) -> None:
    evidence = classify_verification_evidence(
        command,
        exit_code=0,
        output="ok\n",
        real_execution=True,
    )

    assert evidence.category == VerificationEvidenceCategory.REPO_NATIVE
    assert evidence.allowed_to_satisfy_contract is True


@pytest.mark.parametrize(
    "command",
    [
        "pytest --collect-only",
        "pytest -q --co",
        "python --version",
        "pytest --help",
        "go test -run '^$' ./...",
    ],
)
def test_non_executing_repo_native_forms_do_not_count(command: str) -> None:
    evidence = classify_verification_evidence(
        command,
        exit_code=0,
        output="collected 0 items\n",
    )

    assert evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION
    assert evidence.allowed_to_satisfy_contract is False


def test_changed_python_script_counts_as_task_acceptance(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")

    evidence = classify_verification_evidence(
        "python check.py",
        changed_paths={"check.py"},
        exit_code=0,
        output="ok\n",
        root=tmp_path,
    )

    assert evidence.category == VerificationEvidenceCategory.TASK_ACCEPTANCE
    assert evidence.allowed_to_satisfy_contract is True
    assert evidence.reason == "changed_repo_local_script_execution"


def test_changed_r_script_counts_as_task_acceptance(tmp_path: Path) -> None:
    (tmp_path / "solution.R").write_text("print('ok')\n", encoding="utf-8")

    evidence = classify_verification_evidence(
        "Rscript solution.R",
        changed_paths={"solution.R"},
        exit_code=0,
        output="ok\n",
        root=tmp_path,
    )

    assert evidence.category == VerificationEvidenceCategory.TASK_ACCEPTANCE
    assert evidence.allowed_to_satisfy_contract is True


def test_validation_script_path_counts_as_task_acceptance(tmp_path: Path) -> None:
    (tmp_path / "checks" / "validate_output.py").parent.mkdir()
    (tmp_path / "checks" / "validate_output.py").write_text("print('ok')\n", encoding="utf-8")

    evidence = classify_verification_evidence(
        "python checks/validate_output.py",
        exit_code=0,
        output="ok\n",
        root=tmp_path,
    )

    assert evidence.category == VerificationEvidenceCategory.TASK_ACCEPTANCE
    assert evidence.reason == "repo_local_validation_script"


@pytest.mark.parametrize("command", ["diff expected.txt actual.txt", "cmp expected.bin actual.bin"])
def test_diff_and_cmp_count_as_task_acceptance(command: str) -> None:
    evidence = classify_verification_evidence(command, exit_code=0, output="")

    assert evidence.category == VerificationEvidenceCategory.TASK_ACCEPTANCE
    assert evidence.allowed_to_satisfy_contract is True
    assert evidence.reason == "real_output_comparison"


def test_arbitrary_interpreter_invocation_without_context_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "unrelated.py").write_text("print('ok')\n", encoding="utf-8")

    evidence = classify_verification_evidence(
        "python unrelated.py",
        exit_code=0,
        output="ok\n",
        root=tmp_path,
    )

    assert evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION
    assert evidence.allowed_to_satisfy_contract is False


@pytest.mark.parametrize(
    "command",
    [
        "echo success",
        "cat output.txt",
        "ls",
        "pwd",
        "python --version",
        "pytest --collect-only",
        "bash -lc 'pytest -q || true'",
    ],
)
def test_false_positive_commands_do_not_count(command: str) -> None:
    evidence = classify_verification_evidence(command, exit_code=0, output="success\n")

    assert evidence.allowed_to_satisfy_contract is False


def test_material_mutating_verifier_is_not_allowed_to_satisfy_contract() -> None:
    evidence = classify_verification_evidence(
        "pytest -q",
        known_verification_commands=["pytest -q"],
        exit_code=0,
        output="ok\n",
        real_execution=True,
        material_touched_paths={"src/app.py"},
    )

    assert evidence.category == VerificationEvidenceCategory.REPO_NATIVE
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.reason == "mutated_material_paths"


def test_boolean_shell_helper_preserves_contract_matching_behavior() -> None:
    assert _shell_command_is_verification_attempt(
        "pytest tests/test_cli.py -q",
        known_verification_commands=["pytest -q"],
    )
    assert not _shell_command_is_verification_attempt(
        "python check.py",
        known_verification_commands=["pytest -q"],
    )
    assert not _shell_command_is_verification_attempt(
        "true",
        known_verification_commands=["true"],
    )
    assert _shell_command_is_verification_attempt(
        "python check.py",
        known_verification_commands=None,
    )


def test_acceptance_provenance_identifies_preexisting_checker(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Implement the requested behavior.",
    )

    origin = classify_evidence_origin(
        contract=contract,
        command="python check.py",
        touched_paths=set(),
    )

    assert origin == EvidenceOrigin.PREEXISTING_TASK_CHECKER


def test_successful_test_claim_accepts_fresh_observed_execution_evidence() -> None:
    state = TurnExecutionState(execution_requested=True)
    state.note_verification_relevant_edit()
    evidence = classify_verification_evidence(
        "pytest tests/test_app.py -q",
        exit_code=0,
        output="1 passed\n",
        real_execution=True,
    )
    state.record_verification_evidence(
        evidence,
        accepted=evidence.allowed_to_satisfy_contract,
        observed_exit_code=0,
        observed_output=True,
    )

    assert _successful_verification_claim_kind("All tests passed.") == "tests"
    assert _fresh_executed_evidence_for_claim(state, claim_kind="tests")


def test_successful_test_claim_rejects_stale_or_unobserved_execution_evidence() -> None:
    state = TurnExecutionState(execution_requested=True)
    evidence = classify_verification_evidence(
        "pytest tests/test_app.py -q",
        exit_code=0,
        output="1 passed\n",
        real_execution=True,
    )
    state.record_verification_evidence(
        evidence,
        accepted=True,
        observed_exit_code=0,
        observed_output=True,
    )
    state.note_verification_relevant_edit()

    assert _fresh_executed_evidence_for_claim(state, claim_kind="tests") == []

    state.record_verification_evidence(
        evidence,
        accepted=True,
        observed_exit_code=0,
        observed_output=False,
    )
    assert _fresh_executed_evidence_for_claim(state, claim_kind="tests") == []


def test_claim_detector_ignores_truthful_non_execution_reports() -> None:
    assert _successful_verification_claim_kind("I verified the behavior.") == "verification"
    assert _successful_verification_claim_kind("I did not run tests.") is None
    assert (
        _successful_verification_claim_kind("Tests could not run; verification impossible.") is None
    )
    assert _successful_verification_claim_kind("The change was not verified.") is None
    assert _successful_verification_claim_kind("No tests passed because collection failed.") is None


def test_test_claim_accepts_repo_specific_test_runner_execution() -> None:
    state = TurnExecutionState(execution_requested=True)
    evidence = classify_verification_evidence(
        "python tests/runtests.py migrations.test_loader",
        exit_code=0,
        output="Ran 4 tests\nOK\n",
        real_execution=True,
    )
    state.record_verification_evidence(
        evidence,
        accepted=False,
        observed_exit_code=0,
        observed_output=True,
    )

    claim = "Tests: python tests/runtests.py migrations.test_loader (passed)."
    assert _successful_verification_claim_kind(claim) == "tests"
    assert _fresh_executed_evidence_for_claim(state, claim_kind="tests")
