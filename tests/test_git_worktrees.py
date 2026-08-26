from __future__ import annotations

import subprocess
from pathlib import Path

from alysis_code.git_worktrees import ensure_task_worktree, remove_task_worktree


def _cp(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _git_args(cmd: list[str]) -> list[str]:
    assert cmd[0] == "git"
    assert cmd[1] == "-C"
    args = cmd[3:]
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-c":
            i += 2
            continue
        cleaned.append(args[i])
        i += 1
    return cleaned


def test_ensure_task_worktree_creates_new_branch_when_missing(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "wt" / "T01" / "repo"
    seen: list[list[str]] = []
    seen_cmds: list[list[str]] = []
    monkeypatch.setenv("ALYSIS_GIT_HOOKS_PATH", str(tmp_path / "hooks"))

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        seen_cmds.append(list(cmd))
        args = _git_args(cmd)
        seen.append(args)
        if args == ["rev-parse", "--git-dir"]:
            return _cp(returncode=0, stdout=".git\n")
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/feat/t01-a"]:
            return _cp(returncode=1)
        if args == ["worktree", "add", "-b", "feat/t01-a", str(worktree.resolve()), "main"]:
            return _cp(returncode=0)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr("alysis_code.git_worktrees.ensure_git_available", lambda: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    info = ensure_task_worktree(
        root=repo,
        worktree_repo_path=worktree,
        branch="feat/t01-a",
        base_branch="main",
    )
    assert info.created_branch is True
    assert info.reused_existing_worktree is False
    assert ["worktree", "add", "-b", "feat/t01-a", str(worktree.resolve()), "main"] in seen
    assert any(any(str(part).startswith("core.hooksPath=") for part in cmd) for cmd in seen_cmds)


def test_ensure_task_worktree_uses_existing_branch(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "wt" / "T02" / "repo"

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["rev-parse", "--git-dir"]:
            return _cp(returncode=0, stdout=".git\n")
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/feat/t02-b"]:
            return _cp(returncode=0)
        if args == ["worktree", "add", str(worktree.resolve()), "feat/t02-b"]:
            return _cp(returncode=0)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr("alysis_code.git_worktrees.ensure_git_available", lambda: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    info = ensure_task_worktree(
        root=repo,
        worktree_repo_path=worktree,
        branch="feat/t02-b",
        base_branch="main",
    )
    assert info.created_branch is False
    assert info.reused_existing_worktree is False


def test_remove_task_worktree_passes_force(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "wt" / "T03" / "repo"
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        seen.append(args)
        if args == ["worktree", "remove", str(worktree), "--force"]:
            return _cp(returncode=0)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    remove_task_worktree(root=repo, worktree_repo_path=worktree, force=True)
    assert ["worktree", "remove", str(worktree), "--force"] in seen


def test_ensure_task_worktree_recreates_when_existing_path_is_not_git_repo(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "wt" / "T04" / "repo"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "not_git.txt").write_text("x\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        seen.append(args)
        if args == ["rev-parse", "--git-dir"]:
            return _cp(returncode=0, stdout=".git\n")
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return _cp(returncode=1, stderr="not a git repo")
        if args == ["worktree", "remove", str(worktree.resolve()), "--force"]:
            return _cp(returncode=0)
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/feat/t04-c"]:
            return _cp(returncode=1)
        if args == ["worktree", "add", "-b", "feat/t04-c", str(worktree.resolve()), "main"]:
            return _cp(returncode=0)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr("alysis_code.git_worktrees.ensure_git_available", lambda: None)
    monkeypatch.setattr(
        "alysis_code.git_worktrees.ensure_runtime_artifact_excludes",
        lambda _root: None,
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    info = ensure_task_worktree(
        root=repo,
        worktree_repo_path=worktree,
        branch="feat/t04-c",
        base_branch="main",
    )
    assert info.reused_existing_worktree is False
    assert info.created_branch is True
    assert ["worktree", "remove", str(worktree.resolve()), "--force"] in seen
    assert ["worktree", "add", "-b", "feat/t04-c", str(worktree.resolve()), "main"] in seen


def test_ensure_task_worktree_recreates_when_existing_branch_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "wt" / "T05" / "repo"
    worktree.mkdir(parents=True, exist_ok=True)
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        seen.append(args)
        if args == ["rev-parse", "--git-dir"]:
            return _cp(returncode=0, stdout=".git\n")
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return _cp(returncode=0, stdout="true\n")
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _cp(returncode=0, stdout="feat/other\n")
        if args == ["worktree", "remove", str(worktree.resolve()), "--force"]:
            return _cp(returncode=0)
        if args == ["show-ref", "--verify", "--quiet", "refs/heads/feat/t05-c"]:
            return _cp(returncode=1)
        if args == ["worktree", "add", "-b", "feat/t05-c", str(worktree.resolve()), "main"]:
            return _cp(returncode=0)
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr("alysis_code.git_worktrees.ensure_git_available", lambda: None)
    monkeypatch.setattr(
        "alysis_code.git_worktrees.ensure_runtime_artifact_excludes",
        lambda _root: None,
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    info = ensure_task_worktree(
        root=repo,
        worktree_repo_path=worktree,
        branch="feat/t05-c",
        base_branch="main",
    )
    assert info.reused_existing_worktree is False
    assert info.created_branch is True
    assert ["worktree", "remove", str(worktree.resolve()), "--force"] in seen
    assert ["worktree", "add", "-b", "feat/t05-c", str(worktree.resolve()), "main"] in seen


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
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


def test_ensure_task_worktree_recreates_dirty_existing_worktree_without_losing_branch_head(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    worktree = tmp_path / "wt" / "T06" / "repo"

    first = ensure_task_worktree(
        root=repo,
        worktree_repo_path=worktree,
        branch="feat/t06-d",
        base_branch="main",
    )
    assert first.reused_existing_worktree is False

    (worktree / "task.txt").write_text("committed progress\n", encoding="utf-8")
    _git(worktree, "add", "task.txt")
    _git(
        worktree,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "progress",
    )
    preserved_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    (worktree / "stale.txt").write_text("leftover\n", encoding="utf-8")

    second = ensure_task_worktree(
        root=repo,
        worktree_repo_path=worktree,
        branch="feat/t06-d",
        base_branch="main",
    )

    assert second.reused_existing_worktree is False
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == preserved_head
    assert not (worktree / "stale.txt").exists()
    assert (worktree / "task.txt").read_text(encoding="utf-8") == "committed progress\n"
