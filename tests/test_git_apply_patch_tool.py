from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from alysis_code.tools import git as git_module
from alysis_code.tools.git import GitError, git_apply_patch


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_patch_repo(repo: Path) -> Path:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    target = repo / "hello.txt"
    target.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")
    _git(repo, "commit", "-m", "init")
    return target


def test_git_apply_patch_normalizes_crlf_and_applies(tmp_path: Path) -> None:
    target = _init_patch_repo(tmp_path)
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    ).replace("\n", "\r\n")

    result = git_apply_patch(root=tmp_path, patch=patch)

    assert result["applied"] is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_git_apply_patch_rejects_patch_without_headers(tmp_path: Path) -> None:
    _init_patch_repo(tmp_path)

    with pytest.raises(GitError, match="malformed patch: no file paths found"):
        git_apply_patch(
            root=tmp_path,
            patch="@@ -1 +1 @@\n-old\n+new\n",
        )


def test_git_apply_patch_rejects_placeholder_hunk_header(tmp_path: Path) -> None:
    _init_patch_repo(tmp_path)
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ ... @@\n"
        "-old\n"
        "+new\n"
    )

    with pytest.raises(GitError, match="placeholder hunk header"):
        git_apply_patch(root=tmp_path, patch=patch)


def test_git_apply_patch_preflight_blocks_invalid_context_without_mutation(
    tmp_path: Path,
) -> None:
    target = _init_patch_repo(tmp_path)
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1 @@\n"
        "-not-old\n"
        "+new\n"
    )

    with pytest.raises(GitError, match="git apply preflight failed"):
        git_apply_patch(root=tmp_path, patch=patch)

    assert target.read_text(encoding="utf-8") == "old\n"


def test_git_apply_patch_rejects_file_changed_between_final_check_and_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _init_patch_repo(tmp_path)
    target.write_text("old\nstable\n", encoding="utf-8")
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1,2 +1,2 @@\n"
        "-old\n"
        "+agent\n"
        " stable\n"
    )
    original_run = git_module._run_git_checked
    mutating_apply_started = False

    def inject_human_edit_before_apply(**kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal mutating_apply_started
        args = kwargs.get("args")
        if isinstance(args, list) and "apply" in args and "--check" not in args:
            mutating_apply_started = True
            # Change a line outside the patch's replacement. Plain contextual
            # `git apply` would merge this; the whole-file CAS must reject it.
            target.write_text("old\nhuman\n", encoding="utf-8")
        return original_run(**kwargs)

    monkeypatch.setattr(git_module, "_run_git_checked", inject_human_edit_before_apply)

    with pytest.raises(GitError, match="stale_file: hello.txt"):
        git_apply_patch(root=tmp_path, patch=patch)

    assert mutating_apply_started is True
    assert target.read_text(encoding="utf-8") == "old\nhuman\n"


def test_git_apply_patch_serializes_conflicting_concurrent_calls(tmp_path: Path) -> None:
    target = _init_patch_repo(tmp_path)
    patches = [
        (
            "diff --git a/hello.txt b/hello.txt\n"
            "--- a/hello.txt\n"
            "+++ b/hello.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            f"+{replacement}\n"
        )
        for replacement in ("first", "second")
    ]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def apply(patch: str) -> None:
        barrier.wait(timeout=5)
        try:
            git_apply_patch(root=tmp_path, patch=patch)
        except GitError:
            outcomes.append("stale")
        else:
            outcomes.append("applied")

    threads = [threading.Thread(target=apply, args=(patch,)) for patch in patches]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["applied", "stale"]
    assert target.read_text(encoding="utf-8") in {"first\n", "second\n"}


def test_git_apply_patch_private_cas_index_does_not_change_developer_index(
    tmp_path: Path,
) -> None:
    target = _init_patch_repo(tmp_path)
    target.write_text("unstaged\n", encoding="utf-8")
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1 @@\n"
        "-unstaged\n"
        "+patched\n"
    )
    cached_before = _git(tmp_path, "diff", "--cached", "--", "hello.txt").stdout

    git_apply_patch(root=tmp_path, patch=patch)

    assert target.read_text(encoding="utf-8") == "patched\n"
    assert _git(tmp_path, "diff", "--cached", "--", "hello.txt").stdout == cached_before
