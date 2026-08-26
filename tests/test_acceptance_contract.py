from __future__ import annotations

from pathlib import Path

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionConfidence,
    AcceptanceCriterionEnforcement,
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    AcceptancePathKind,
    AcceptancePathRole,
    EvidenceOrigin,
    build_acceptance_contract,
    classify_evidence_origin,
    finalize_acceptance_contract,
    record_acceptance_tool_effect,
)
from alysis_code.repo_scan import RepoScanResult


def _repo_scan(
    root: Path,
    *,
    likely_test_commands: list[str] | None = None,
    observed_paths: list[str] | None = None,
) -> RepoScanResult:
    return RepoScanResult(
        schema_version=1,
        workspace_root=str(root),
        focus_relpath="",
        workspace_kind="repo",
        git_root=None,
        has_head_commit=False,
        current_branch=None,
        top_level_entries=[],
        manifests=[],
        readme_paths=[],
        readme_excerpts=[],
        conventions_path=None,
        conventions_excerpt=None,
        language_hints=[],
        package_hints=[],
        likely_test_commands=likely_test_commands or [],
        observed_paths=observed_paths or [],
    )


def _criterion_kinds(contract) -> set[AcceptanceCriterionKind]:  # type: ignore[no-untyped-def]
    return {criterion.kind for criterion in contract.criteria}


def _first_criterion(contract, kind: AcceptanceCriterionKind):  # type: ignore[no-untyped-def]
    matches = [criterion for criterion in contract.criteria if criterion.kind == kind]
    assert matches
    return matches[0]


def test_acceptance_contract_extracts_core_criteria(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction=(
            "Only create out/result.json, keep README.md unchanged, keep the "
            "service running on port 8080, require accuracy at least 95%, "
            "and run `diff expected.json out/result.json`."
        ),
        authoritative_verification_commands=["python host_check.py"],
        effective_verification_commands=["pytest -q"],
    )

    kinds = _criterion_kinds(contract)

    assert AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH in kinds
    assert AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA in kinds
    assert AcceptanceCriterionKind.EXPLICIT_COMMAND_IO in kinds
    assert AcceptanceCriterionKind.PERSISTENT_SERVICE in kinds
    assert AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH in kinds
    assert AcceptanceCriterionKind.THRESHOLD in kinds
    assert AcceptanceCriterionKind.EXPLICIT_HOST_USER_VERIFICATION_COMMAND in kinds
    assert AcceptanceCriterionKind.PREEXISTING_REPO_CHECK_SURFACE in kinds
    assert AcceptanceCriterionKind.FUNCTIONAL_API_PROTOCOL in kinds
    assert "out/result.json" in contract.allowed_output_paths


def test_absolute_workspace_output_resolves_to_relative_path() -> None:
    contract = build_acceptance_contract(
        root=Path("/app"),
        instruction="Create /app/regex.txt.",
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH)
    assert criterion.paths == ("regex.txt",)
    assert criterion.path_refs[0].path_kind == AcceptancePathKind.ABSOLUTE_WITHIN_WORKSPACE
    assert criterion.path_refs[0].workspace_relative_path == "regex.txt"
    assert criterion.path_refs[0].role == AcceptancePathRole.REQUIRED_OUTPUT
    assert contract.allowed_output_paths == {"regex.txt"}


def test_windows_absolute_workspace_output_is_parsed_cross_platform() -> None:
    contract = build_acceptance_contract(
        root=Path("C:/workspace/app"),
        instruction=r"Create C:\workspace\app\regex.txt.",
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH)
    assert criterion.paths == ("regex.txt",)
    assert criterion.path_refs[0].path_kind == AcceptancePathKind.ABSOLUTE_WITHIN_WORKSPACE
    assert criterion.path_refs[0].workspace_relative_path == "regex.txt"
    assert criterion.path_refs[0].absolute_path == "C:/workspace/app/regex.txt"


def test_absolute_workspace_output_finalization_uses_workspace_relative_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "regex.txt").write_text("ok\n", encoding="utf-8")
    contract = build_acceptance_contract(
        root=root,
        instruction=f"Create {root}/regex.txt.",
    )

    finalize_acceptance_contract(contract=contract, root=root, touched_paths={"regex.txt"})

    criterion = _first_criterion(contract, AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH)
    assert criterion.paths == ("regex.txt",)
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert not (root / root.name / "regex.txt").exists()


def test_relative_and_external_paths_keep_distinct_models(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Create results/out.txt and create /usr/local/bin/tool.",
    )

    required = [
        criterion
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    ]

    assert [criterion.paths for criterion in required] == [
        ("results/out.txt",),
        ("/usr/local/bin/tool",),
    ]
    assert required[0].path_refs[0].path_kind == AcceptancePathKind.WORKSPACE_RELATIVE
    assert required[1].path_refs[0].path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL
    assert required[1].path_refs[0].absolute_path == "/usr/local/bin/tool"
    assert contract.allowed_output_paths == {"results/out.txt"}


def test_preservation_clause_keeps_dotted_filename_and_does_not_require_output() -> None:
    contract = build_acceptance_contract(
        root=Path("/app"),
        instruction="Do not modify /app/model_ref.xml.",
    )

    preserve = _first_criterion(contract, AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH)
    assert preserve.paths == ("model_ref.xml",)
    assert preserve.path_refs[0].role == AcceptancePathRole.PRESERVATION_TARGET
    assert preserve.enforcement == AcceptanceCriterionEnforcement.HARD
    assert not [
        criterion
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    ]
    assert not [
        criterion
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA
    ]


def test_input_paths_are_advisory_references_not_required_outputs(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Read from input/data.csv and write result.txt.",
    )

    references = [
        criterion
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.REFERENCE_PATH
    ]
    required = [
        criterion
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    ]

    assert references
    assert references[0].paths == ("input/data.csv",)
    assert references[0].path_refs[0].role == AcceptancePathRole.EXISTING_INPUT
    assert references[0].enforcement == AcceptanceCriterionEnforcement.ADVISORY
    assert references[0].required is False
    assert [criterion.paths for criterion in required] == [("result.txt",)]


def test_explicit_output_format_is_path_scoped_and_hard(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Create result.txt. Output must be valid JSON.",
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA)

    assert criterion.paths == ("result.txt",)
    assert criterion.enforcement == AcceptanceCriterionEnforcement.HARD
    assert criterion.confidence == AcceptanceCriterionConfidence.EXPLICIT
    assert criterion.required is True
    assert criterion.required_for_finalization is True


def test_weak_ambiguous_path_reference_is_advisory(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Compare with reference model_ref.xml.",
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.REFERENCE_PATH)

    assert criterion.paths == ("model_ref.xml",)
    assert criterion.enforcement == AcceptanceCriterionEnforcement.ADVISORY
    assert criterion.confidence == AcceptanceCriterionConfidence.HEURISTIC
    assert criterion.required is False
    assert "model_ref.xml" not in contract.allowed_output_paths


def test_extension_only_output_format_is_advisory(tmp_path: Path) -> None:
    contract = build_acceptance_contract(root=tmp_path, instruction="Create result.json.")

    output = _first_criterion(contract, AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH)
    format_criterion = _first_criterion(contract, AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA)

    assert output.enforcement == AcceptanceCriterionEnforcement.HARD
    assert format_criterion.paths == ("result.json",)
    assert format_criterion.enforcement == AcceptanceCriterionEnforcement.ADVISORY
    assert format_criterion.required_for_finalization is False


def test_exact_user_black_box_command_satisfies_its_criterion(tmp_path: Path) -> None:
    (tmp_path / "expected.txt").write_text("ok\n", encoding="utf-8")
    (tmp_path / "actual.txt").write_text("ok\n", encoding="utf-8")
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Create actual.txt and run `diff expected.txt actual.txt`.",
    )

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_run",
        arguments={"cmd": "diff expected.txt actual.txt"},
        status="ok",
        result={
            "effective_cmd": "diff expected.txt actual.txt",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        },
        touched_paths=set(),
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.EXPLICIT_COMMAND_IO)
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert contract.evidence[-1].origin == EvidenceOrigin.USER_EXPLICIT


def test_inline_python_acceptance_snippet_becomes_python_command(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction=(
            "Validate with this Python snippet: `from math import sqrt; assert sqrt(4) == 2`."
        ),
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.EXPLICIT_COMMAND_IO)

    assert criterion.commands
    assert criterion.commands[0].startswith("python -c ")
    assert "from math import sqrt" in criterion.commands[0]
    assert "/bin/sh" not in criterion.commands[0]


def test_backtick_prose_does_not_become_hard_command(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Use the `best effort` behavior and remember `not a command`.",
    )

    assert not [
        criterion
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.EXPLICIT_COMMAND_IO
    ]


def test_self_authored_test_is_supplemental_for_repo_surface(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Implement the requested behavior.",
        effective_verification_commands=["pytest -q"],
    )

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="verify_run",
        arguments={},
        status="ok",
        result={
            "commands": ["pytest -q"],
            "all_passed": True,
            "command_results": [
                {"command": "pytest -q", "exit_code": 0, "ok": True, "output_preview": "1 passed"}
            ],
        },
        touched_paths={"tests/test_generated.py"},
        known_verification_commands=["pytest -q"],
        evidence_allowed=True,
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.PREEXISTING_REPO_CHECK_SURFACE)
    assert contract.evidence[-1].origin == EvidenceOrigin.SELF_AUTHORED
    assert criterion.status == AcceptanceCriterionStatus.BLOCKED
    assert "Self-authored" in criterion.failure_summary


def test_preexisting_repo_native_check_can_provide_primary_evidence(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Implement the requested behavior.",
        effective_verification_commands=["pytest -q"],
        repo_scan=_repo_scan(
            tmp_path,
            likely_test_commands=["pytest -q"],
            observed_paths=["tests/test_app.py"],
        ),
    )

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="verify_run",
        arguments={},
        status="ok",
        result={
            "commands": ["pytest -q"],
            "all_passed": True,
            "command_results": [
                {"command": "pytest -q", "exit_code": 0, "ok": True, "output_preview": "1 passed"}
            ],
        },
        touched_paths=set(),
        known_verification_commands=["pytest -q"],
        evidence_allowed=True,
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.PREEXISTING_REPO_CHECK_SURFACE)
    assert contract.evidence[-1].origin == EvidenceOrigin.PREEXISTING_REPO_NATIVE
    assert criterion.status == AcceptanceCriterionStatus.PASSED


def test_threshold_failure_blocks_and_self_authored_pass_does_not_override(
    tmp_path: Path,
) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="The solution must report accuracy at least 90%.",
        effective_verification_commands=["pytest -q"],
    )

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_run",
        arguments={"cmd": "python eval.py"},
        status="ok",
        result={"effective_cmd": "python eval.py", "exit_code": 0, "stdout": "accuracy 72\n"},
        touched_paths=set(),
    )
    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="verify_run",
        arguments={},
        status="ok",
        result={
            "commands": ["pytest -q"],
            "all_passed": True,
            "command_results": [
                {
                    "command": "pytest -q",
                    "exit_code": 0,
                    "ok": True,
                    "output_preview": "accuracy 100 100",
                }
            ],
        },
        touched_paths={"tests/test_generated.py"},
        known_verification_commands=["pytest -q"],
        evidence_allowed=True,
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.THRESHOLD)
    assert criterion.status == AcceptanceCriterionStatus.FAILED
    assert "misses" in criterion.failure_summary


def test_performance_threshold_requires_multiple_samples(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="The latency must stay under 50ms.",
    )

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_run",
        arguments={"cmd": "python bench.py"},
        status="ok",
        result={"effective_cmd": "python bench.py", "exit_code": 0, "stdout": "latency 40ms"},
        touched_paths=set(),
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.THRESHOLD)
    assert criterion.status == AcceptanceCriterionStatus.BLOCKED
    assert "insufficient repeated samples" in criterion.failure_summary


def test_missing_output_and_preservation_change_block_finalization(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("keep\n", encoding="utf-8")
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Only create result.txt. Do not touch README.md.",
    )

    finalize_acceptance_contract(
        contract=contract,
        root=tmp_path,
        touched_paths={"README.md", "extra.txt"},
    )

    problems = set(contract.problem_names())
    assert "acceptance_criteria_unverified" in problems
    assert "acceptance_criteria_failed" in problems
    assert "unexpected_scope_changes" in problems


def test_allowed_output_path_does_not_create_scope_violation(tmp_path: Path) -> None:
    (tmp_path / "result.txt").write_text("ok\n", encoding="utf-8")
    contract = build_acceptance_contract(root=tmp_path, instruction="Only create result.txt.")

    finalize_acceptance_contract(
        contract=contract,
        root=tmp_path,
        touched_paths={"result.txt"},
    )

    assert "unexpected_scope_changes" not in contract.problem_names()
    assert not contract.problem_names()


def test_path_like_natural_language_categories_are_not_required_artifacts(
    tmp_path: Path,
) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Υλοποίησε αυτή την αλλαγή και ενημέρωσε docs/tests.",
    )

    paths = {path for criterion in contract.criteria for path in criterion.paths}

    assert "docs/tests" not in paths
    assert "docs/tests." not in paths
    assert "docs/tests" not in contract.allowed_output_paths


def test_same_session_service_evidence_does_not_satisfy_persistence(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Keep the service running on port 8080.",
    )

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_run",
        arguments={"cmd": "curl http://localhost:8080/health"},
        status="ok",
        result={
            "effective_cmd": "curl http://localhost:8080/health",
            "exit_code": 0,
            "stdout": "ok\n",
        },
        touched_paths=set(),
    )
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())

    criterion = _first_criterion(contract, AcceptanceCriterionKind.PERSISTENT_SERVICE)
    assert criterion.status == AcceptanceCriterionStatus.BLOCKED
    assert "durable" in criterion.failure_summary


def test_shell_background_does_not_satisfy_persistent_service(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Keep the service running on port 8080.",
    )

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_background",
        arguments={"cmd": "python -m http.server 8080"},
        status="ok",
        result={"process_id": "bg_1", "status": "running"},
        touched_paths=set(),
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.PERSISTENT_SERVICE)
    assert criterion.status == AcceptanceCriterionStatus.BLOCKED
    assert "session-owned" in criterion.failure_summary


def test_durable_service_evidence_satisfies_persistent_service_after_recheck(
    tmp_path: Path,
) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Keep the service running on port 8080.",
    )
    ready_payload = {
        "service_id": "svc_ready",
        "ownership": "DURABLE_SERVICE",
        "status": "running",
        "alive": True,
        "readiness": {"type": "tcp", "status": "ready", "port": 8080},
    }

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_service_start",
        arguments={"cmd": "python -m http.server 8080"},
        status="ok",
        result=ready_payload,
        touched_paths=set(),
    )
    finalize_acceptance_contract(
        contract=contract,
        root=tmp_path,
        touched_paths=set(),
        durable_service_status=lambda _service_id: ready_payload,
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.PERSISTENT_SERVICE)
    protocol = _first_criterion(contract, AcceptanceCriterionKind.FUNCTIONAL_API_PROTOCOL)
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert protocol.status == AcceptanceCriterionStatus.PASSED
    assert criterion.service_ids == ["svc_ready"]


def test_durable_service_wrong_port_remains_blocked(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Keep the service running on port 8080.",
    )
    payload = {
        "service_id": "svc_wrong",
        "ownership": "DURABLE_SERVICE",
        "status": "running",
        "alive": True,
        "readiness": {"type": "tcp", "status": "ready", "port": 9090},
    }

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_service_start",
        arguments={"cmd": "python -m http.server 9090"},
        status="ok",
        result=payload,
        touched_paths=set(),
    )
    finalize_acceptance_contract(
        contract=contract,
        root=tmp_path,
        touched_paths=set(),
        durable_service_status=lambda _service_id: payload,
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.PERSISTENT_SERVICE)
    assert criterion.status == AcceptanceCriterionStatus.BLOCKED
    assert "readiness=tcp/ready" in criterion.failure_summary


def test_durable_service_finalization_recheck_must_still_be_ready(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Keep the service running on port 8080.",
    )
    initial_payload = {
        "service_id": "svc_flaky",
        "ownership": "DURABLE_SERVICE",
        "status": "running",
        "alive": True,
        "readiness": {"type": "tcp", "status": "ready", "port": 8080},
    }
    final_payload = {
        "service_id": "svc_flaky",
        "ownership": "DURABLE_SERVICE",
        "status": "exited",
        "alive": False,
        "readiness": {"type": "tcp", "status": "failed", "port": 8080},
    }

    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_service_start",
        arguments={"cmd": "python -m http.server 8080"},
        status="ok",
        result=initial_payload,
        touched_paths=set(),
    )
    finalize_acceptance_contract(
        contract=contract,
        root=tmp_path,
        touched_paths=set(),
        durable_service_status=lambda _service_id: final_payload,
    )

    criterion = _first_criterion(contract, AcceptanceCriterionKind.PERSISTENT_SERVICE)
    assert criterion.status == AcceptanceCriterionStatus.BLOCKED
    assert "recheck failed" in criterion.failure_summary


def test_created_checker_path_is_classified_self_authored(tmp_path: Path) -> None:
    contract = build_acceptance_contract(root=tmp_path, instruction="Implement and check it.")

    origin = classify_evidence_origin(
        contract=contract,
        command="python tests/test_generated.py",
        touched_paths={"tests/test_generated.py"},
    )

    assert origin == EvidenceOrigin.SELF_AUTHORED


def test_authoritative_custom_checker_keeps_preexisting_origin_when_unchanged(
    tmp_path: Path,
) -> None:
    checker = tmp_path / "oracle.sh"
    checker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Implement and verify it.",
        authoritative_verification_commands=["./oracle.sh"],
    )

    origin = classify_evidence_origin(
        contract=contract,
        root=tmp_path,
        command="./oracle.sh",
        touched_paths=set(),
    )

    assert origin == EvidenceOrigin.PREEXISTING_TASK_CHECKER


def test_authoritative_custom_checker_modified_after_snapshot_is_self_authored(
    tmp_path: Path,
) -> None:
    checker = tmp_path / "oracle.sh"
    checker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Implement and verify it.",
        authoritative_verification_commands=["./oracle.sh"],
    )
    checker.write_text("#!/bin/sh\nexit 0 # modified\n", encoding="utf-8")

    origin = classify_evidence_origin(
        contract=contract,
        root=tmp_path,
        command="./oracle.sh",
        touched_paths=set(),
        verification_authoritative=True,
        known_verification_commands=["./oracle.sh"],
    )

    assert origin == EvidenceOrigin.SELF_AUTHORED
