from __future__ import annotations

from pathlib import Path

from alysis_code.agent.acceptance_contract import (
    build_acceptance_contract,
    finalize_acceptance_contract,
)
from alysis_code.agent_loop import (
    MutationPathCategory,
    _run_with_command_mutation_detection,
    benign_runtime_mutation_paths,
    classify_mutation_path,
    material_mutation_paths,
)
from alysis_code.runtime_artifacts import is_runtime_artifact_path


def _write(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_runtime_cache_paths_are_benign(tmp_path: Path) -> None:
    paths = [
        ".pytest_cache/v/cache/nodeids",
        "pkg/__pycache__/mod.cpython-312.pyc",
        ".mypy_cache/3.12/meta.json",
        ".ruff_cache/CACHEDIR.TAG",
        ".coverage",
    ]

    assert all(is_runtime_artifact_path(path, root=tmp_path) for path in paths)
    assert benign_runtime_mutation_paths(paths, root=tmp_path) == sorted(paths)
    assert material_mutation_paths(paths, root=tmp_path) == []


def test_coverage_directory_is_not_broadly_ignored(tmp_path: Path) -> None:
    assert is_runtime_artifact_path("coverage/index.html", root=tmp_path) is False
    classification = classify_mutation_path("coverage/index.html", root=tmp_path)

    assert classification.category != MutationPathCategory.BENIGN_RUNTIME_ARTIFACT
    assert classification.is_material is True


def test_shell_created_deliverables_with_unfamiliar_extensions_are_material(
    tmp_path: Path,
) -> None:
    for rel_path in ("data.comp", "out.html", "artifact.weirdext"):
        classification = classify_mutation_path(
            rel_path,
            root=tmp_path,
            existed_before=False,
        )
        assert classification.category == MutationPathCategory.MATERIAL_DELIVERABLE
        assert classification.is_material is True


def test_unknown_existing_non_runtime_path_falls_back_to_material(tmp_path: Path) -> None:
    classification = classify_mutation_path("reports/output.bin", root=tmp_path)

    assert classification.category == MutationPathCategory.UNKNOWN_MATERIAL
    assert classification.is_material is True


def test_command_mutation_detection_ignores_only_benign_runtime_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    def operation() -> dict[str, bool]:
        _write(root / ".pytest_cache" / "v" / "cache" / "nodeids")
        _write(root / "pkg" / "__pycache__" / "mod.cpython-312.pyc")
        _write(root / ".coverage")
        _write(root / "data.comp")
        _write(root / "out.html")
        return {"ok": True}

    result, touched = _run_with_command_mutation_detection(
        root=root,
        enabled=True,
        operation=operation,
    )

    assert result == {"ok": True}
    assert touched == ["data.comp", "out.html"]


def test_failed_command_mutation_detection_keeps_material_change(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    def operation() -> None:
        _write(root / "artifact.weirdext")

    result, touched = _run_with_command_mutation_detection(
        root=root,
        enabled=True,
        operation=operation,
    )

    assert result is None
    assert touched == ["artifact.weirdext"]


def test_allowed_acceptance_output_remains_material_without_scope_violation(
    tmp_path: Path,
) -> None:
    (tmp_path / "result.txt").write_text("ok\n", encoding="utf-8")
    contract = build_acceptance_contract(root=tmp_path, instruction="Only create result.txt.")

    finalize_acceptance_contract(
        contract=contract,
        root=tmp_path,
        touched_paths={"result.txt"},
    )

    assert material_mutation_paths(["result.txt"], root=tmp_path) == ["result.txt"]
    assert contract.problem_names() == []
