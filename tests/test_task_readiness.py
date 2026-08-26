from __future__ import annotations

from alysis_code.task_readiness import (
    TASK_KIND_ANALYSIS_ONLY,
    TASK_KIND_IMPLEMENTATION,
    TASK_KIND_TEST_ONLY,
    TASK_KIND_VERIFICATION_ONLY,
    ZERO_DIFF_ANALYSIS_OK,
    classify_task_lifecycle,
    normalize_task_file_fields,
)


def test_classifies_read_current_task_as_analysis_only_despite_code_paths() -> None:
    lifecycle = classify_task_lifecycle(
        title="Read current config_loader.py and test_config_loader.py",
        description="Inspect the current files before implementation tasks run.",
        acceptance_criteria=["Findings are available for the next task."],
        estimated_files=["config_loader.py", "test_config_loader.py"],
        write_scope=["config_loader.py", "test_config_loader.py"],
    )

    assert lifecycle.kind == TASK_KIND_ANALYSIS_ONLY
    assert lifecycle.zero_diff_policy == ZERO_DIFF_ANALYSIS_OK


def test_normalize_task_file_fields_clears_read_only_mutation_scope() -> None:
    scope = normalize_task_file_fields(
        title="Read current src/ConfigLoader.java and tests",
        description="Read-only inspection; report findings.",
        acceptance_criteria=["Findings documented."],
        estimated_files=["src/ConfigLoader.java", "src/test/java/ConfigLoaderTest.java"],
        write_scope=["src/ConfigLoader.java", "src/test/java/ConfigLoaderTest.java"],
        warning_prefix="Task T01",
    )

    assert scope.estimated_files == []
    assert scope.write_scope == []
    assert scope.requires_runnable_scope is False
    assert scope.task_kind == TASK_KIND_ANALYSIS_ONLY
    assert any("cleared file mutation scope" in warning for warning in scope.warnings)


def test_classifies_java_test_only_scope_separately_from_implementation() -> None:
    lifecycle = classify_task_lifecycle(
        title="Add regression tests",
        description="Cover parser failures.",
        acceptance_criteria=["Tests fail before the implementation task."],
        estimated_files=["src/test/java/com/example/ParserTest.java"],
        write_scope=["src/test/java/com/example/ParserTest.java"],
    )

    assert lifecycle.kind == TASK_KIND_TEST_ONLY
    assert lifecycle.requires_runnable_scope is True


def test_classifies_scoped_code_work_as_implementation() -> None:
    lifecycle = classify_task_lifecycle(
        title="Fix parser behavior",
        description="Update implementation.",
        acceptance_criteria=["Parser handles quoted values."],
        estimated_files=["src/main/java/com/example/Parser.java"],
        write_scope=["src/main/java/com/example/Parser.java"],
    )

    assert lifecycle.kind == TASK_KIND_IMPLEMENTATION


def test_classifies_run_tests_as_verification_only_without_scope() -> None:
    lifecycle = classify_task_lifecycle(
        title="Run tests",
        description="Check the current repo state.",
        acceptance_criteria=["Verification result recorded."],
        estimated_files=[],
        write_scope=[],
    )

    assert lifecycle.kind == TASK_KIND_VERIFICATION_ONLY
    assert lifecycle.requires_runnable_scope is False
