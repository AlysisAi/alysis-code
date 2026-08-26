from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import alysis_code.swarm_backend as swarm_backend
import alysis_code.workspace_isolation as workspace_isolation
from alysis_code.forge import make_run_paths
from alysis_code.git_ops import (
    GitOpsError,
    branch_exists,
    current_branch,
    delete_branch,
    has_head_commit,
    merge_no_ff,
)
from alysis_code.git_worktrees import (
    ensure_task_worktree,
    prune_worktrees,
    remove_task_worktree,
)
from alysis_code.swarm_backend import (
    GitWorktreeSwarmBackend,
    SnapshotSwarmBackend,
    select_swarm_backend,
)


@pytest.mark.parametrize(
    "helper_name",
    (
        "_run_git_checked",
        "_inspect_existing_git_workspace",
        "_sanitize_existing_git_workspace",
        "_reset_git_workspace_to_target",
        "_cleanup_workspace_path",
        "_retry_readonly_removal",
    ),
)
def test_workspace_isolation_helpers_remain_reexported(helper_name: str) -> None:
    assert getattr(swarm_backend, helper_name) is getattr(workspace_isolation, helper_name)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_head(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "init",
    )


def test_git_worktree_backend_reports_expected_name_and_capabilities() -> None:
    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=lambda **_kwargs: None,
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_args, **_kwargs: None,
        prune_fn=lambda _root: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
        has_head_commit_fn=lambda _root: True,
    )

    assert backend.name == "git_worktree"
    assert backend.capabilities == frozenset({"git", "merge", "worktree"})
    assert backend.requires_head_commit is True
    assert backend.supports_remote_sync is True


def test_git_worktree_backend_delegates_to_current_helpers(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    calls: dict[str, object] = {}

    def fake_prune(repo_root: Path) -> None:
        calls["prune"] = repo_root

    def fake_current_branch(repo_root: Path) -> str:
        calls["current_branch"] = repo_root
        return "main"

    def fake_has_head_commit(repo_root: Path) -> bool:
        calls["has_head_commit"] = repo_root
        return True

    def fake_branch_exists(repo_root: Path, branch: str) -> bool:
        calls["branch_exists"] = (repo_root, branch)
        return True

    def fake_ensure_worktree(**kwargs: object) -> None:
        calls["ensure_worktree"] = kwargs

    def fake_merge_runner(
        repo_root: Path,
        *,
        base_branch: str,
        task_branch: str,
        message: str,
    ) -> str:
        calls["merge_runner"] = (repo_root, base_branch, task_branch, message)
        return "merge-123"

    def fake_remove_worktree(**kwargs: object) -> None:
        calls["remove_worktree"] = kwargs
        worktree_path = Path(str(kwargs["worktree_repo_path"]))
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
        worktree_path = Path(str(kwargs["worktree_repo_path"]))
        if worktree_path.exists():
            worktree_path.rmdir()
        worktree_path = Path(str(kwargs["worktree_repo_path"]))
        if worktree_path.exists():
            worktree_path.rmdir()
        worktree_path = Path(str(kwargs["worktree_repo_path"]))
        if worktree_path.exists():
            worktree_path.rmdir()

    def fake_delete_branch(repo_root: Path, branch: str) -> None:
        calls["delete_branch"] = (repo_root, branch)

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=fake_ensure_worktree,
        merge_runner=fake_merge_runner,
        remove_worktree_fn=fake_remove_worktree,
        delete_branch_fn=fake_delete_branch,
        prune_fn=fake_prune,
        branch_exists_fn=fake_branch_exists,
        current_branch_fn=fake_current_branch,
        has_head_commit_fn=fake_has_head_commit,
    )

    startup = backend.prepare_startup(root)
    assert startup.warnings == []
    assert calls["prune"] == root
    assert backend.default_base_branch(root) == "main"
    assert calls["current_branch"] == root
    assert backend.has_head_commit(root) is True
    assert calls["has_head_commit"] == root
    assert backend.branch_exists(root, "feat/t01-demo") is True
    assert calls["branch_exists"] == (root, "feat/t01-demo")

    prepared = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )
    assert prepared.worktree_path == run_dir / "worktrees" / "T01" / "repo"
    assert prepared.control_root == root
    assert calls["ensure_worktree"] == {
        "root": root,
        "worktree_repo_path": prepared.worktree_path,
        "branch": "feat/t01-demo",
        "base_branch": "main",
    }

    prepared.worktree_path.mkdir(parents=True)
    apply_result = backend.apply_task_success(
        root=root,
        prepared_workspace=prepared,
        message="Merge T01: Demo task",
        changed_files=["src/demo.py"],
    )
    assert apply_result.merge_commit_hash == "merge-123"
    assert apply_result.action == "merged"
    assert calls["merge_runner"] == (
        root,
        "main",
        "feat/t01-demo",
        "Merge T01: Demo task",
    )

    cleanup_errors = backend.cleanup_task_workspace(
        root=root,
        prepared_workspace=prepared,
        keep_worktrees=False,
    )
    assert cleanup_errors == []
    assert calls["remove_worktree"] == {
        "root": root,
        "worktree_repo_path": prepared.worktree_path,
        "force": True,
    }
    assert calls["delete_branch"] == (root, "feat/t01-demo")


def test_git_worktree_backend_reports_prune_warning(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=lambda **_kwargs: None,
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_args, **_kwargs: None,
        prune_fn=lambda _root: (_ for _ in ()).throw(RuntimeError("boom")),
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
        has_head_commit_fn=lambda _root: True,
    )

    startup = backend.prepare_startup(root)
    assert startup.warnings == ["git worktree prune failed: boom"]


def test_git_worktree_backend_prepares_candidate_workspace_and_merges_into_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    calls: dict[str, object] = {}
    branch_exists_state = {"feat/t01-demo": True, "alysis-candidate/run-1/batch_001": True}

    def fake_ensure_worktree(**kwargs: object) -> None:
        calls.setdefault("ensure_worktree", []).append(kwargs)

    def fake_merge_runner(
        repo_root: Path,
        *,
        base_branch: str,
        task_branch: str,
        message: str,
    ) -> str:
        calls["merge_runner"] = (repo_root, base_branch, task_branch, message)
        return "merge-123"

    def fake_remove_worktree(**kwargs: object) -> None:
        calls["remove_worktree"] = kwargs
        worktree_path = Path(str(kwargs["worktree_repo_path"]))
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

    def fake_delete_branch(repo_root: Path, branch: str) -> None:
        calls["delete_branch"] = (repo_root, branch)
        branch_exists_state[branch] = False

    def fake_reset_git_workspace_to_target(*, worktree_path: Path, target: str) -> None:
        calls["reset_candidate"] = (worktree_path, target)

    def fake_require_materialized_candidate_workspace(
        *,
        worktree_path: Path,
        expected_branch: str,
    ) -> None:
        calls["validated_candidate"] = (worktree_path, expected_branch)

    monkeypatch.setattr(
        "alysis_code.swarm_backend._reset_git_workspace_to_target",
        fake_reset_git_workspace_to_target,
    )
    monkeypatch.setattr(
        "alysis_code.swarm_backend._require_materialized_candidate_workspace",
        fake_require_materialized_candidate_workspace,
    )

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=fake_ensure_worktree,
        merge_runner=fake_merge_runner,
        remove_worktree_fn=fake_remove_worktree,
        delete_branch_fn=fake_delete_branch,
        prune_fn=lambda _root: None,
        branch_exists_fn=lambda _root, branch: branch_exists_state.get(branch, False),
        current_branch_fn=lambda _root: "main",
        has_head_commit_fn=lambda _root: True,
    )

    task_workspace = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )
    candidate_workspace = backend.prepare_candidate_workspace(
        root=root,
        run_dir=run_dir,
        batch_label="batch_001",
        branch="alysis-candidate/run-1/batch_001",
        base_branch="main",
    )

    assert candidate_workspace.worktree_path == (
        run_dir / "worktrees" / "_batch_candidates" / "batch_001" / "repo"
    )
    assert candidate_workspace.control_root == candidate_workspace.worktree_path
    assert calls["validated_candidate"] == (
        candidate_workspace.worktree_path,
        "alysis-candidate/run-1/batch_001",
    )
    assert calls["reset_candidate"] == (candidate_workspace.worktree_path, "main")

    backend.apply_task_to_candidate(
        root=root,
        candidate_workspace=candidate_workspace,
        prepared_workspace=task_workspace,
        message="Merge T01: Demo task",
        changed_files=["src/demo.py"],
    )

    assert calls["merge_runner"] == (
        candidate_workspace.worktree_path,
        "alysis-candidate/run-1/batch_001",
        "feat/t01-demo",
        "Merge T01: Demo task",
    )

    candidate_workspace.worktree_path.mkdir(parents=True, exist_ok=True)
    cleanup_errors = backend.cleanup_candidate_workspace(
        root=root,
        candidate_workspace=candidate_workspace,
        keep_worktrees=False,
    )

    assert cleanup_errors == []
    assert calls["remove_worktree"] == {
        "root": root,
        "worktree_repo_path": candidate_workspace.worktree_path,
        "force": True,
    }
    assert calls["delete_branch"] == (root, "alysis-candidate/run-1/batch_001")


def test_git_worktree_backend_reuses_candidate_path_but_resets_branch_to_base(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    _init_repo_with_head(root)
    _git(root, "checkout", "-b", "feat/t01-demo")
    (root / "task.txt").write_text("task change\n", encoding="utf-8")
    _git(root, "add", "task.txt")
    _git(
        root,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "task",
    )
    _git(root, "checkout", "main")

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=ensure_task_worktree,
        merge_runner=merge_no_ff,
        remove_worktree_fn=remove_task_worktree,
        delete_branch_fn=delete_branch,
        prune_fn=prune_worktrees,
        branch_exists_fn=branch_exists,
        current_branch_fn=current_branch,
        has_head_commit_fn=has_head_commit,
    )

    task_workspace = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )
    candidate_workspace = backend.prepare_candidate_workspace(
        root=root,
        run_dir=run_dir,
        batch_label="batch_001",
        branch="alysis-candidate/run-1/batch_001",
        base_branch="main",
    )
    base_head = _git(candidate_workspace.worktree_path, "rev-parse", "HEAD").stdout.strip()

    backend.apply_task_to_candidate(
        root=root,
        candidate_workspace=candidate_workspace,
        prepared_workspace=task_workspace,
        message="Merge T01: Demo task",
        changed_files=["task.txt"],
    )
    merged_head = _git(candidate_workspace.worktree_path, "rev-parse", "HEAD").stdout.strip()
    assert merged_head != base_head
    assert (candidate_workspace.worktree_path / "task.txt").exists()

    reused_candidate_workspace = backend.prepare_candidate_workspace(
        root=root,
        run_dir=run_dir,
        batch_label="batch_001",
        branch="alysis-candidate/run-1/batch_001",
        base_branch="main",
    )
    reused_head = _git(reused_candidate_workspace.worktree_path, "rev-parse", "HEAD").stdout.strip()
    assert reused_head == base_head
    assert not (reused_candidate_workspace.worktree_path / "task.txt").exists()
    assert _git(reused_candidate_workspace.worktree_path, "status", "--porcelain").stdout == ""


def test_git_worktree_backend_requires_materialized_candidate_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    _init_repo_with_head(root)

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=lambda **_kwargs: None,
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_args, **_kwargs: None,
        prune_fn=prune_worktrees,
        branch_exists_fn=branch_exists,
        current_branch_fn=current_branch,
        has_head_commit_fn=has_head_commit,
    )

    with pytest.raises(
        GitOpsError,
        match="candidate workspace was not materialized by ensure_worktree_fn",
    ):
        backend.prepare_candidate_workspace(
            root=root,
            run_dir=run_dir,
            batch_label="batch_001",
            branch="alysis-candidate/run-1/batch_001",
            base_branch="main",
        )


def test_git_worktree_backend_failure_cleanup_removes_worktree_and_deletes_branch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    calls: dict[str, object] = {}
    branch_present = {"value": True}

    def fake_remove_worktree(**kwargs: object) -> None:
        calls["remove_worktree"] = kwargs
        worktree_path = Path(str(kwargs["worktree_repo_path"]))
        worktree_path.rmdir()

    def fake_delete_branch(repo_root: Path, branch: str) -> None:
        calls["delete_branch"] = (repo_root, branch)
        branch_present["value"] = False

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=lambda **_kwargs: None,
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        remove_worktree_fn=fake_remove_worktree,
        delete_branch_fn=fake_delete_branch,
        prune_fn=lambda _root: None,
        branch_exists_fn=lambda _root, _branch: branch_present["value"],
        current_branch_fn=lambda _root: "main",
        has_head_commit_fn=lambda _root: True,
    )

    prepared = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )
    prepared.worktree_path.mkdir(parents=True)

    cleanup_errors = backend.cleanup_failed_task_workspace(
        root=root,
        prepared_workspace=prepared,
        keep_worktrees=False,
    )

    assert cleanup_errors == []
    assert calls["remove_worktree"] == {
        "root": root,
        "worktree_repo_path": prepared.worktree_path,
        "force": True,
    }
    assert calls["delete_branch"] == (root, "feat/t01-demo")


def test_git_worktree_backend_failure_cleanup_force_deletes_rejected_branch_and_rerun_starts_clean(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    _init_repo_with_head(root)

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=ensure_task_worktree,
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        remove_worktree_fn=remove_task_worktree,
        delete_branch_fn=delete_branch,
        prune_fn=prune_worktrees,
        branch_exists_fn=branch_exists,
        current_branch_fn=current_branch,
        has_head_commit_fn=has_head_commit,
    )

    base_head = _git(root, "rev-parse", "main").stdout.strip()
    prepared = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )
    (prepared.worktree_path / "rejected.txt").write_text("rejected change\n", encoding="utf-8")
    _git(prepared.worktree_path, "add", "rejected.txt")
    _git(
        prepared.worktree_path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "rejected",
    )
    rejected_head = _git(prepared.worktree_path, "rev-parse", "HEAD").stdout.strip()

    assert rejected_head != base_head
    assert branch_exists(root, "feat/t01-demo") is True

    cleanup_errors = backend.cleanup_failed_task_workspace(
        root=root,
        prepared_workspace=prepared,
        keep_worktrees=False,
    )

    assert cleanup_errors == []
    assert not prepared.worktree_path.exists()
    assert branch_exists(root, "feat/t01-demo") is False

    rerun = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )

    assert _git(rerun.worktree_path, "rev-parse", "HEAD").stdout.strip() == base_head
    assert not (rerun.worktree_path / "rejected.txt").exists()


def test_git_worktree_backend_failure_cleanup_keeps_debug_state_when_requested(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    _init_repo_with_head(root)

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=ensure_task_worktree,
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        remove_worktree_fn=remove_task_worktree,
        delete_branch_fn=delete_branch,
        prune_fn=prune_worktrees,
        branch_exists_fn=branch_exists,
        current_branch_fn=current_branch,
        has_head_commit_fn=has_head_commit,
    )

    prepared = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )
    (prepared.worktree_path / "debug.txt").write_text("keep me\n", encoding="utf-8")
    _git(prepared.worktree_path, "add", "debug.txt")
    _git(
        prepared.worktree_path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "debug",
    )
    debug_head = _git(prepared.worktree_path, "rev-parse", "HEAD").stdout.strip()

    cleanup_errors = backend.cleanup_failed_task_workspace(
        root=root,
        prepared_workspace=prepared,
        keep_worktrees=True,
    )

    assert cleanup_errors == []
    assert prepared.worktree_path.exists()
    assert branch_exists(root, "feat/t01-demo") is True
    assert _git(prepared.worktree_path, "rev-parse", "HEAD").stdout.strip() == debug_head
    assert (prepared.worktree_path / "debug.txt").read_text(encoding="utf-8") == "keep me\n"


def test_git_worktree_backend_prepare_blocks_when_previous_failed_cleanup_left_residue(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    _init_repo_with_head(root)

    def fail_remove_worktree(**_kwargs: object) -> None:
        raise RuntimeError("simulated remove failure")

    backend = GitWorktreeSwarmBackend(
        ensure_worktree_fn=ensure_task_worktree,
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        remove_worktree_fn=fail_remove_worktree,
        delete_branch_fn=delete_branch,
        prune_fn=prune_worktrees,
        branch_exists_fn=branch_exists,
        current_branch_fn=current_branch,
        has_head_commit_fn=has_head_commit,
    )

    prepared = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="main",
    )
    (prepared.worktree_path / "rejected.txt").write_text("rejected change\n", encoding="utf-8")
    _git(prepared.worktree_path, "add", "rejected.txt")
    _git(
        prepared.worktree_path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "rejected",
    )

    cleanup_errors = backend.cleanup_failed_task_workspace(
        root=root,
        prepared_workspace=prepared,
        keep_worktrees=False,
    )

    assert cleanup_errors
    marker_path = prepared.worktree_path.parent / "failed_cleanup.json"
    assert marker_path.exists()

    with pytest.raises(
        GitOpsError,
        match="previous failed task cleanup left unresolved git-worktree state",
    ):
        backend.prepare_task_workspace(
            root=root,
            run_dir=run_dir,
            task_id="T01",
            branch="feat/t01-demo",
            base_branch="main",
        )


def test_snapshot_backend_initializes_repo_and_applies_changes_back(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    (root / "src.txt").write_text("before\n", encoding="utf-8")
    (root / "cli.pyc").write_bytes(b"parent-pyc")
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "mod.cpython-310.pyc").write_bytes(b"parent-cache")

    backend = SnapshotSwarmBackend(
        merge_runner=lambda repo_root, **kwargs: subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        branch_exists_fn=lambda repo_root, branch: (
            subprocess.run(
                ["git", "-C", repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        ),
        current_branch_fn=lambda _root: "snapshot-base",
    )

    prepared = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="snapshot-base",
    )
    assert prepared.control_root == prepared.worktree_path
    assert backend.branch_exists(prepared.control_root, "feat/t01-demo") is True
    tracked = _git(prepared.worktree_path, "ls-files").stdout.splitlines()
    assert "cli.pyc" not in tracked
    assert "pkg/__pycache__/mod.cpython-310.pyc" not in tracked
    assert not (prepared.worktree_path / "cli.pyc").exists()
    assert not (prepared.worktree_path / "pkg" / "__pycache__").exists()

    (prepared.worktree_path / "src.txt").write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "-C", prepared.worktree_path, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            prepared.worktree_path,
            "-c",
            "user.name=alysis-agent",
            "-c",
            "user.email=alysis-agent@local",
            "commit",
            "-m",
            "task update",
        ],
        check=True,
    )

    applied = backend.apply_task_success(
        root=root,
        prepared_workspace=prepared,
        message="Merge T01: Demo task",
        changed_files=["src.txt"],
    )
    assert applied.action == "applied"
    assert (root / "src.txt").read_text(encoding="utf-8") == "after\n"


def test_snapshot_backend_prepares_candidate_workspace_and_applies_task_files_into_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    (root / "src.txt").write_text("before\n", encoding="utf-8")

    backend = SnapshotSwarmBackend(
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        branch_exists_fn=lambda repo_root, branch: (
            subprocess.run(
                ["git", "-C", repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        ),
        current_branch_fn=lambda _root: "snapshot-base",
    )

    task_workspace = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="snapshot-base",
    )
    (task_workspace.worktree_path / "src.txt").write_text("after\n", encoding="utf-8")

    candidate_workspace = backend.prepare_candidate_workspace(
        root=root,
        run_dir=run_dir,
        batch_label="batch_001",
        branch="candidate/batch_001",
        base_branch="snapshot-base",
    )

    backend.apply_task_to_candidate(
        root=root,
        candidate_workspace=candidate_workspace,
        prepared_workspace=task_workspace,
        message="ignored",
        changed_files=["src.txt"],
    )

    assert (candidate_workspace.worktree_path / "src.txt").read_text(encoding="utf-8") == "after\n"
    assert (root / "src.txt").read_text(encoding="utf-8") == "before\n"

    cleanup_errors = backend.cleanup_candidate_workspace(
        root=root,
        candidate_workspace=candidate_workspace,
        keep_worktrees=False,
    )

    assert cleanup_errors == []
    assert not candidate_workspace.worktree_path.exists()


def test_snapshot_backend_rebuilds_reusable_workspace_with_tracked_runtime_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    (root / "src.txt").write_text("before\n", encoding="utf-8")

    backend = SnapshotSwarmBackend(
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        branch_exists_fn=lambda repo_root, branch: (
            subprocess.run(
                ["git", "-C", repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        ),
        current_branch_fn=lambda _root: "snapshot-base",
    )

    first = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="snapshot-base",
    )
    (first.worktree_path / "src.txt").write_text("committed change\n", encoding="utf-8")
    _git(first.worktree_path, "add", "src.txt")
    _git(
        first.worktree_path,
        "-c",
        "user.name=alysis-agent",
        "-c",
        "user.email=alysis-agent@local",
        "commit",
        "-m",
        "progress",
    )
    contaminated_head = _git(first.worktree_path, "rev-parse", "HEAD").stdout.strip()
    (first.worktree_path / "cli.pyc").write_bytes(b"tracked-pyc")
    _git(first.worktree_path, "add", "-f", "cli.pyc")
    _git(
        first.worktree_path,
        "-c",
        "user.name=alysis-agent",
        "-c",
        "user.email=alysis-agent@local",
        "commit",
        "-m",
        "track runtime artifact",
    )
    tracked_before = _git(first.worktree_path, "ls-files").stdout.splitlines()
    assert "cli.pyc" in tracked_before

    second = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="snapshot-base",
    )

    assert second.worktree_path == first.worktree_path
    assert _git(second.worktree_path, "rev-parse", "HEAD").stdout.strip() != contaminated_head
    assert (second.worktree_path / "src.txt").read_text(encoding="utf-8") == "before\n"
    assert "cli.pyc" not in _git(second.worktree_path, "ls-files").stdout.splitlines()
    assert not (second.worktree_path / "cli.pyc").exists()


def test_snapshot_backend_prepare_reuses_dirty_workspace_after_sanitizing(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    (root / "src.txt").write_text("before\n", encoding="utf-8")

    backend = SnapshotSwarmBackend(
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        branch_exists_fn=lambda repo_root, branch: (
            subprocess.run(
                ["git", "-C", repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        ),
        current_branch_fn=lambda _root: "snapshot-base",
    )

    first = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="snapshot-base",
    )
    (first.worktree_path / "src.txt").write_text("committed change\n", encoding="utf-8")
    _git(first.worktree_path, "add", "src.txt")
    _git(
        first.worktree_path,
        "-c",
        "user.name=alysis-agent",
        "-c",
        "user.email=alysis-agent@local",
        "commit",
        "-m",
        "progress",
    )
    preserved_head = _git(first.worktree_path, "rev-parse", "HEAD").stdout.strip()
    (first.worktree_path / "stale.txt").write_text("leftover\n", encoding="utf-8")

    second = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="snapshot-base",
    )

    assert second.worktree_path == first.worktree_path
    assert _git(second.worktree_path, "rev-parse", "HEAD").stdout.strip() == preserved_head
    assert (second.worktree_path / "src.txt").read_text(encoding="utf-8") == "committed change\n"
    assert not (second.worktree_path / "stale.txt").exists()


def test_snapshot_backend_failure_cleanup_sanitizes_to_branch_head(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    (root / "src.txt").write_text("before\n", encoding="utf-8")

    backend = SnapshotSwarmBackend(
        merge_runner=lambda *_args, **_kwargs: "merge-commit",
        branch_exists_fn=lambda repo_root, branch: (
            subprocess.run(
                ["git", "-C", repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        ),
        current_branch_fn=lambda _root: "snapshot-base",
    )

    prepared = backend.prepare_task_workspace(
        root=root,
        run_dir=run_dir,
        task_id="T01",
        branch="feat/t01-demo",
        base_branch="snapshot-base",
    )
    (prepared.worktree_path / "src.txt").write_text("committed change\n", encoding="utf-8")
    _git(prepared.worktree_path, "add", "src.txt")
    _git(
        prepared.worktree_path,
        "-c",
        "user.name=alysis-agent",
        "-c",
        "user.email=alysis-agent@local",
        "commit",
        "-m",
        "progress",
    )
    preserved_head = _git(prepared.worktree_path, "rev-parse", "HEAD").stdout.strip()
    (prepared.worktree_path / "src.txt").write_text("dirty tracked\n", encoding="utf-8")
    (prepared.worktree_path / "stale.txt").write_text("leftover\n", encoding="utf-8")

    cleanup_errors = backend.cleanup_failed_task_workspace(
        root=root,
        prepared_workspace=prepared,
        keep_worktrees=False,
    )

    assert cleanup_errors == []
    assert _git(prepared.worktree_path, "rev-parse", "HEAD").stdout.strip() == preserved_head
    assert (prepared.worktree_path / "src.txt").read_text(encoding="utf-8") == "committed change\n"
    assert not (prepared.worktree_path / "stale.txt").exists()


def test_select_swarm_backend_uses_snapshot_for_plain_dir(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    paths = make_run_paths(
        root=root, run_id="r1", workspace_kind="plain_dir", has_head_commit=False
    )

    backend = select_swarm_backend(
        paths=paths,
        ensure_worktree_fn=lambda **_kwargs: None,
        merge_runner=lambda *_args, **_kwargs: "merge",
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_args, **_kwargs: None,
        prune_fn=lambda _root: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
        has_head_commit_fn=lambda _root: True,
    )

    assert isinstance(backend, SnapshotSwarmBackend)


def test_select_swarm_backend_uses_git_worktree_for_repo_with_head(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    paths = make_run_paths(root=root, run_id="r1", workspace_kind="git_repo", has_head_commit=True)

    backend = select_swarm_backend(
        paths=paths,
        ensure_worktree_fn=lambda **_kwargs: None,
        merge_runner=lambda *_args, **_kwargs: "merge",
        remove_worktree_fn=lambda **_kwargs: None,
        delete_branch_fn=lambda *_args, **_kwargs: None,
        prune_fn=lambda _root: None,
        branch_exists_fn=lambda _root, _branch: True,
        current_branch_fn=lambda _root: "main",
        has_head_commit_fn=lambda _root: True,
    )

    assert isinstance(backend, GitWorktreeSwarmBackend)
