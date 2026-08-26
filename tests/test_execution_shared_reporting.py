from __future__ import annotations

import os
import subprocess
from pathlib import Path

import alysis_code.execution_shared as shared


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


def _git_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("env", _git_env())
    kwargs.setdefault("timeout", 10)
    return subprocess.run(args, **kwargs)


def _init_git_repo_with_commit(repo: Path) -> None:
    _git_run(
        ["git", "-C", os.fspath(repo.parent), "init", os.fspath(repo.name)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_build_execution_reporting_diff_sorts_filters_and_dedupes_paths(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        shared,
        "_tracked_patch_text",
        lambda _root: "diff --git a/README.md b/README.md\n@@ -1 +1 @@\n-repo\n+repo2\n",
    )
    monkeypatch.setattr(
        shared,
        "_git_status_entries",
        lambda _root: [
            shared._GitStatusEntry(status=" M", path="README.md"),
            shared._GitStatusEntry(
                status="R ", path="src/new_name.py", orig_path="src/old_name.py"
            ),
            shared._GitStatusEntry(status=" D", path="docs/obsolete.md"),
            shared._GitStatusEntry(status="M ", path="README.md"),
        ],
    )
    monkeypatch.setattr(
        shared,
        "_git_untracked_files",
        lambda _root: [
            "docs/new.md",
            "docs/new.md",
            ".alysis/runs/current.json",
        ],
    )
    monkeypatch.setattr(
        shared,
        "_untracked_file_patch_text",
        lambda *, root, rel_path: f"diff --git a/{rel_path} b/{rel_path}\nnew file mode 100644\n",
    )

    result = shared.build_execution_reporting_diff(tmp_path)

    assert result.changed_files == (
        "README.md",
        "docs/new.md",
        "docs/obsolete.md",
        "src/new_name.py",
        "src/old_name.py",
    )
    assert result.patch_text.count("diff --git a/docs/new.md b/docs/new.md") == 1
    assert ".alysis/runs/current.json" not in result.patch_text


def test_build_execution_reporting_diff_filters_untracked_egg_info_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(shared, "_tracked_patch_text", lambda _root: "")
    monkeypatch.setattr(shared, "_git_status_entries", lambda _root: [])
    monkeypatch.setattr(shared.shutil, "which", lambda _cmd: "/usr/bin/git")

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        if args[-4:] == ["ls-files", "--others", "--exclude-standard", "-z"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=(
                    b"pyproject.toml\0"
                    b"src/calcbox/core.py\0"
                    b"src/calcbox.egg-info/PKG-INFO\0"
                    b"src/calcbox.egg-info/SOURCES.txt\0"
                ),
                stderr=b"",
            )
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(shared.subprocess, "run", fake_run)
    monkeypatch.setattr(
        shared,
        "_untracked_file_patch_text",
        lambda *, root, rel_path: f"diff --git a/{rel_path} b/{rel_path}\nnew file mode 100644\n",
    )

    result = shared.build_execution_reporting_diff(tmp_path)

    assert result.changed_files == ("pyproject.toml", "src/calcbox/core.py")
    assert "calcbox.egg-info/PKG-INFO" not in result.patch_text
    assert "calcbox.egg-info/SOURCES.txt" not in result.patch_text


def test_build_execution_reporting_diff_includes_staged_tracked_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)

    (repo / "README.md").write_text("repo\nupdated\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = shared.build_execution_reporting_diff(repo)

    assert result.changed_files == ("README.md",)
    assert "diff --git a/README.md b/README.md" in result.patch_text


def test_build_execution_reporting_diff_includes_staged_new_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)

    (repo / "staged_new.py").write_text("print('hi')\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "staged_new.py"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = shared.build_execution_reporting_diff(repo)

    assert result.changed_files == ("staged_new.py",)
    assert "diff --git a/staged_new.py b/staged_new.py" in result.patch_text
    assert any(f"new file mode {mode}" in result.patch_text for mode in ("100644", "100755"))


def test_build_workspace_snapshot_reporting_diff_reports_plain_dir_changes(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "keep.txt").write_text("base\n", encoding="utf-8")
    (repo / "delete.txt").write_text("delete me\n", encoding="utf-8")
    before = shared.snapshot_workspace_tree(repo)

    (repo / "keep.txt").write_text("updated\n", encoding="utf-8")
    (repo / "delete.txt").unlink()
    (repo / "new.txt").write_text("new file\n", encoding="utf-8")
    after = shared.snapshot_workspace_tree(repo)

    result = shared.build_workspace_snapshot_reporting_diff(
        repo,
        before_snapshot=before,
        after_snapshot=after,
    )

    assert result.changed_files == ("delete.txt", "keep.txt", "new.txt")
    assert "modified: keep.txt" in result.patch_text
    assert "deleted: delete.txt" in result.patch_text
    assert "added: new.txt" in result.patch_text


def test_build_workspace_snapshot_reporting_diff_ignores_rust_target_runtime_artifacts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    before = shared.snapshot_workspace_tree(repo)

    (repo / "src" / "main.rs").write_text('fn main() { println!("hi"); }\n', encoding="utf-8")
    (repo / "target" / "debug").mkdir(parents=True)
    (repo / "target" / "debug" / "demo").write_bytes(b"bin")
    after = shared.snapshot_workspace_tree(repo)

    result = shared.build_workspace_snapshot_reporting_diff(
        repo,
        before_snapshot=before,
        after_snapshot=after,
    )

    assert result.changed_files == ("src/main.rs",)
    assert "target/debug/demo" not in result.patch_text


def test_build_workspace_snapshot_reporting_diff_keeps_non_rust_target_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    before = shared.snapshot_workspace_tree(repo)

    (repo / "target").mkdir()
    (repo / "target" / "generated.txt").write_text("new file\n", encoding="utf-8")
    after = shared.snapshot_workspace_tree(repo)

    result = shared.build_workspace_snapshot_reporting_diff(
        repo,
        before_snapshot=before,
        after_snapshot=after,
    )

    assert result.changed_files == ("target/generated.txt",)
    assert "added: target/generated.txt" in result.patch_text


def test_build_task_local_workspace_reporting_diff_excludes_preexisting_dirty_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.txt").write_text("bravo\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "a.txt", "b.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    before_commit = _git_run(
        ["git", "-C", os.fspath(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("alpha dirty before task\n", encoding="utf-8")

    baseline = shared.capture_task_local_workspace_baseline(repo, before_commit=before_commit)
    try:
        (repo / "b.txt").write_text("bravo task change\n", encoding="utf-8")
        (repo / "README.md").unlink()
        (repo / "new.txt").write_text("new file\n", encoding="utf-8")

        result = shared.build_task_local_workspace_reporting_diff(
            repo,
            baseline=baseline,
            after_commit=before_commit,
        )
    finally:
        shared.cleanup_task_local_workspace_baseline(baseline)

    assert result.changed_files == ("README.md", "b.txt", "new.txt")
    assert "modified: b.txt" in result.patch_text
    assert any(f"deleted file mode {mode}" in result.patch_text for mode in ("100644", "100755"))
    assert any(f"new file mode {mode}" in result.patch_text for mode in ("100644", "100755"))
    assert "diff --git a/b.txt b/b.txt" in result.patch_text
    assert "a.txt" not in result.patch_text


def test_rewrite_no_index_patch_paths_normalizes_windows_quoted_paths(tmp_path: Path) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "repo"
    before_root.mkdir()
    after_root.mkdir()
    before_display = shared._git_no_index_display_path(before_root).replace("/", "\\\\")
    after_display = shared._git_no_index_display_path(after_root).replace("/", "\\\\")
    patch_text = (
        f'diff --git "a/{before_display}\\b.txt" "b/{after_display}\\b.txt"\n'
        f'--- "a/{before_display}\\b.txt"\n'
        f'+++ "b/{after_display}\\b.txt"\n'
        "@@ -1 +1 @@\n"
        "-bravo\n"
        "+bravo task change\n"
    )

    rewritten = shared._rewrite_no_index_patch_paths(
        patch_text,
        before_root=before_root,
        after_root=after_root,
    )

    assert "diff --git a/b.txt b/b.txt" in rewritten
    assert "--- a/b.txt" in rewritten
    assert "+++ b/b.txt" in rewritten
    assert '"' not in rewritten


def test_build_task_local_workspace_reporting_diff_ignores_clean_start_restore(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)

    before_commit = _git_run(
        ["git", "-C", os.fspath(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    baseline = shared.capture_task_local_workspace_baseline(repo, before_commit=before_commit)
    try:
        (repo / "README.md").write_text("repo\nchanged\n", encoding="utf-8")
        (repo / "README.md").write_text("repo\n", encoding="utf-8")

        result = shared.build_task_local_workspace_reporting_diff(
            repo,
            baseline=baseline,
            after_commit=before_commit,
        )
    finally:
        shared.cleanup_task_local_workspace_baseline(baseline)

    assert result.changed_files == ()
    assert result.patch_text == ""


def test_build_execution_reporting_diff_returns_fallback_when_git_probe_times_out(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GIT_ASKPASS", "interactive-helper")
    monkeypatch.setenv("GCM_INTERACTIVE", "always")
    monkeypatch.setattr(shared.shutil, "which", lambda _cmd: "/usr/bin/git")
    seen_envs: list[dict[str, str]] = []
    seen_timeouts: list[float] = []

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        seen_envs.append(dict(kwargs["env"]))
        seen_timeouts.append(float(kwargs["timeout"]))
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(shared.subprocess, "run", fake_run)

    result = shared.build_execution_reporting_diff(tmp_path)

    assert result.changed_files == ()
    assert result.patch_text == "(no git diff available)\n"
    assert seen_timeouts
    assert all(timeout == shared._READ_ONLY_GIT_TIMEOUT_S for timeout in seen_timeouts)
    assert all(env["GIT_TERMINAL_PROMPT"] == "0" for env in seen_envs)
    assert all(env["GIT_ASKPASS"] == "" for env in seen_envs)
    assert all(env["SSH_ASKPASS"] == "" for env in seen_envs)
    assert all(env["GCM_INTERACTIVE"] == "never" for env in seen_envs)


def test_build_task_local_workspace_reporting_diff_ignores_dirty_start_restore(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.txt").write_text("bravo\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "a.txt", "b.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    before_commit = _git_run(
        ["git", "-C", os.fspath(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("alpha dirty before task\n", encoding="utf-8")

    baseline = shared.capture_task_local_workspace_baseline(repo, before_commit=before_commit)
    try:
        (repo / "b.txt").write_text("bravo task change\n", encoding="utf-8")
        (repo / "b.txt").write_text("bravo\n", encoding="utf-8")

        result = shared.build_task_local_workspace_reporting_diff(
            repo,
            baseline=baseline,
            after_commit=before_commit,
        )
    finally:
        shared.cleanup_task_local_workspace_baseline(baseline)

    assert result.changed_files == ()
    assert result.patch_text == ""
