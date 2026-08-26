from __future__ import annotations

from pathlib import Path

from alysis_code.execution_shared import (
    mirror_plan_into_worktree,
    mirror_selected_knowledge_into_worktree,
    prepare_task_execution_knowledge,
)
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.knowledge_base import write_task_attempt_entry


def test_mirror_plan_into_worktree_skips_unchanged_files(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    source_asset = paths.assets_dir / "brief.txt"
    source_asset.write_text("brief\n", encoding="utf-8")

    worktree_repo = paths.run_dir / "worktrees" / "T01" / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    mirror_plan_into_worktree(run_paths=paths, worktree_repo_path=worktree_repo)

    import alysis_code.execution_shared as shared

    copied: list[tuple[Path, Path]] = []
    original_copy2 = shared.shutil.copy2

    def tracked_copy2(src: Path, dst: Path):  # type: ignore[no-untyped-def]
        copied.append((Path(src), Path(dst)))
        return original_copy2(src, dst)

    monkeypatch.setattr(shared.shutil, "copy2", tracked_copy2)
    mirror_plan_into_worktree(run_paths=paths, worktree_repo_path=worktree_repo)
    assert copied == []


def test_mirror_plan_into_worktree_copies_changed_files(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    source_asset = paths.assets_dir / "brief.txt"
    source_asset.write_text("v1\n", encoding="utf-8")

    worktree_repo = paths.run_dir / "worktrees" / "T01" / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    mirror_plan_into_worktree(run_paths=paths, worktree_repo_path=worktree_repo)

    source_asset.write_text("v2 changed\n", encoding="utf-8")

    import alysis_code.execution_shared as shared

    copied: list[tuple[Path, Path]] = []
    original_copy2 = shared.shutil.copy2

    def tracked_copy2(src: Path, dst: Path):  # type: ignore[no-untyped-def]
        copied.append((Path(src), Path(dst)))
        return original_copy2(src, dst)

    monkeypatch.setattr(shared.shutil, "copy2", tracked_copy2)
    mirror_plan_into_worktree(run_paths=paths, worktree_repo_path=worktree_repo)

    assert copied
    mirrored_asset = (
        worktree_repo / ".alysis" / "runs" / paths.run_id / "plan" / "assets" / "brief.txt"
    )
    assert mirrored_asset.read_text(encoding="utf-8") == "v2 changed\n"


def test_mirror_selected_knowledge_into_worktree_copies_selected_view(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
    )
    current_task = add_task(
        plan,
        title="Follow up parser retry",
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)
    write_task_attempt_entry(
        paths=paths,
        task=prior_task,
        source="forge_exec",
        result="success",
        summary="Prior parser work completed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    prepared = prepare_task_execution_knowledge(
        run_paths=paths,
        task=current_task,
        selection_label="execution",
    )
    worktree_repo = paths.run_dir / "worktrees" / "T02" / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    mirror_selected_knowledge_into_worktree(
        materialized=prepared,
        run_paths=paths,
        worktree_repo_path=worktree_repo,
    )

    mirrored_manifest = (
        worktree_repo
        / ".alysis"
        / "runs"
        / paths.run_id
        / "knowledge"
        / "selected"
        / str(current_task["id"])
        / "execution"
        / "manifest.json"
    )
    assert mirrored_manifest.exists()
