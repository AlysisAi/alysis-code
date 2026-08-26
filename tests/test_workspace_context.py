from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from alysis_code import workspace_context as workspace_context_mod
from alysis_code.workspace_context import WorkspaceContextError, resolve_workspace_context


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_EDITOR"] = "true"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    env["PAGER"] = "cat"
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
        timeout=10,
    )


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")


def _commit_file(repo: Path, relative_path: str = "README.md") -> None:
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello\n", encoding="utf-8")
    _git(repo, "add", relative_path)
    _git(repo, "-c", "commit.gpgsign=false", "commit", "--no-gpg-sign", "--no-verify", "-m", "init")


def test_resolve_workspace_context_for_plain_dir(tmp_path: Path) -> None:
    ctx = resolve_workspace_context(tmp_path)

    assert ctx.input_path == tmp_path.resolve()
    assert ctx.focus_path == tmp_path.resolve()
    assert ctx.workspace_root == tmp_path.resolve()
    assert ctx.git_root is None
    assert ctx.focus_relpath == "."
    assert ctx.workspace_kind == "plain_dir"
    assert ctx.has_head_commit is False
    assert ctx.current_branch is None


def test_resolve_workspace_context_ignores_invalid_parent_git_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = tmp_path / "container"
    workspace = container / "nested" / "workspace"
    workspace.mkdir(parents=True)
    (container / ".git").mkdir()

    def fail_git_probe(*_args, **_kwargs):
        raise AssertionError("invalid parent .git should not trigger git probing")

    monkeypatch.setattr(workspace_context_mod.subprocess, "run", fail_git_probe)

    ctx = resolve_workspace_context(workspace)

    assert ctx.input_path == workspace.resolve()
    assert ctx.workspace_root == workspace.resolve()
    assert ctx.git_root is None
    assert ctx.focus_relpath == "."
    assert ctx.workspace_kind == "plain_dir"


def test_resolve_workspace_context_for_git_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_file(repo)

    ctx = resolve_workspace_context(repo)

    assert ctx.workspace_root == repo.resolve()
    assert ctx.focus_path == repo.resolve()
    assert ctx.git_root == repo.resolve()
    assert ctx.focus_relpath == "."
    assert ctx.workspace_kind == "git_repo"
    assert ctx.has_head_commit is True
    assert ctx.current_branch


def test_resolve_workspace_context_for_git_subdir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_file(repo)
    subdir = repo / "pkg" / "api"
    subdir.mkdir(parents=True)

    ctx = resolve_workspace_context(subdir)

    assert ctx.workspace_root == repo.resolve()
    assert ctx.focus_path == subdir.resolve()
    assert ctx.git_root == repo.resolve()
    assert ctx.focus_relpath == "pkg/api"
    assert ctx.workspace_kind == "git_repo"
    assert ctx.has_head_commit is True


def test_resolve_workspace_context_for_repo_without_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    ctx = resolve_workspace_context(repo)

    assert ctx.workspace_root == repo.resolve()
    assert ctx.git_root == repo.resolve()
    assert ctx.focus_relpath == "."
    assert ctx.workspace_kind == "git_repo_no_head"
    assert ctx.has_head_commit is False
    assert ctx.current_branch


def test_resolve_workspace_context_errors_on_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(WorkspaceContextError, match="does not exist"):
        resolve_workspace_context(missing)


def test_resolve_workspace_context_falls_back_when_git_probe_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_ASKPASS", "interactive-helper")
    monkeypatch.setenv("GCM_INTERACTIVE", "always")
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_file(repo)
    real_run = workspace_context_mod.subprocess.run
    seen_envs: list[dict[str, str]] = []
    seen_timeouts: list[float] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd[:3] == ["git", "-C", os.fspath(repo)]:
            seen_envs.append(dict(kwargs["env"]))
            seen_timeouts.append(float(kwargs["timeout"]))
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(workspace_context_mod.subprocess, "run", fake_run)

    ctx = resolve_workspace_context(repo)

    assert seen_timeouts
    assert all(timeout == workspace_context_mod._GIT_PROBE_TIMEOUT_S for timeout in seen_timeouts)
    assert all(env["GIT_TERMINAL_PROMPT"] == "0" for env in seen_envs)
    assert all(env["GIT_ASKPASS"] == "" for env in seen_envs)
    assert all(env["SSH_ASKPASS"] == "" for env in seen_envs)
    assert all(env["GCM_INTERACTIVE"] == "never" for env in seen_envs)
    assert ctx.workspace_root == repo.resolve()
    assert ctx.git_root == repo.resolve()
    assert ctx.workspace_kind == "git_repo"
    assert ctx.has_head_commit is True
    assert ctx.current_branch
