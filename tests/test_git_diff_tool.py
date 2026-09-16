from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alysis_code.tools.git import GitError, git_diff, git_status


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "core.autocrlf", "false")
    for name in ("first.txt", "literal[1].txt"):
        (tmp_path / name).write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_diff_pages_reconstruct_the_complete_patch(repo: Path) -> None:
    (repo / "first.txt").write_text("line with unicode café " * 3000 + "\n", encoding="utf-8")
    first = page = git_diff(root=repo)
    parts = [page["diff"]]
    assert first["truncated"] is True
    while page["next_offset"] is not None:
        page = git_diff(root=repo, offset=page["next_offset"], diff_id=page["diff_id"])
        assert len(page["diff"]) <= 20000
        parts.append(page["diff"])
    expected = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color")
    assert "".join(parts) == expected
    assert first["total_chars"] == len(expected)
    assert page["truncated"] is False


def test_diff_scopes_literal_paths_and_staged_changes(repo: Path) -> None:
    (repo / "literal[1].txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "literal[1].txt")
    (repo / "first.txt").write_text("unstaged\n", encoding="utf-8")
    assert git_diff(root=repo, path="literal[1].txt")["diff"] == ""
    page = git_diff(root=repo, path="literal[1].txt", staged=True)
    assert "+staged" in page["diff"]
    assert "first.txt" not in page["diff"]
    assert "+unstaged" in git_diff(root=repo)["diff"]
    with pytest.raises(GitError, match="escapes root"):
        git_diff(root=repo, path="../outside")


def test_diff_decodes_unicode_independently_of_windows_locale(repo: Path) -> None:
    content = "Γεια σου κόσμε — café “quoted” 👋\n"
    (repo / "first.txt").write_text(content, encoding="utf-8")
    assert "+" + content in git_diff(root=repo)["diff"]


def test_diff_requires_a_matching_page_identity(repo: Path) -> None:
    (repo / "first.txt").write_text("long line " * 3000, encoding="utf-8")
    page = git_diff(root=repo)
    with pytest.raises(GitError, match="previous page"):
        git_diff(root=repo, offset=20000)
    (repo / "first.txt").write_text("changed again\n", encoding="utf-8")
    with pytest.raises(GitError, match="changed between pages"):
        git_diff(root=repo, offset=20000, diff_id=page["diff_id"])
    for bad_offset in (-1, True, "20000"):
        with pytest.raises(GitError, match="non-negative integer"):
            git_diff(root=repo, offset=bad_offset)  # type: ignore[arg-type]


def test_review_does_not_run_configured_git_programs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "first.txt").write_text("changed\n", encoding="utf-8")
    # Each configured program would leave a marker if Git invoked it.
    program = "echo unexpected > review-hook-ran"
    _git(repo, "config", "core.fsmonitor", program)
    _git(repo, "config", "diff.external", program)
    _git(repo, "config", "diff.review.textconv", program)
    _git(repo, "config", "filter.review.clean", program)
    _git(repo, "config", "filter.review.process", program)
    _git(repo, "config", "filter.review.required", "true")
    (repo / ".gitattributes").write_text("*.txt diff=review filter=review\n", encoding="utf-8")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", program)
    for review in (git_diff, git_status):
        with pytest.raises(GitError, match="git_content_filter_required"):
            review(root=repo)
    assert git_diff(root=repo, staged=True)["diff"] == ""
    assert not (repo / "review-hook-ran").exists()


def test_filtered_unchanged_files_are_never_reported_as_modifications(repo: Path) -> None:
    _git(repo, "config", "filter.demo.clean", "git hash-object --stdin")
    _git(repo, "config", "filter.demo.required", "true")
    (repo / ".gitattributes").write_text("first.txt filter=demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "filtered baseline")
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "diff") == ""
    for review in (git_diff, git_status):
        with pytest.raises(GitError, match="cannot compare these files accurately"):
            review(root=repo)
    assert git_diff(root=repo, staged=True)["diff"] == ""
    # A global filter configuration must not block an unaffected scoped read.
    (repo / "literal[1].txt").write_text("real edit\n", encoding="utf-8")
    assert "+real edit" in git_diff(root=repo, path="literal[1].txt")["diff"]


def test_unused_filter_configuration_keeps_native_review_available(repo: Path) -> None:
    _git(repo, "config", "filter.lfs.clean", "echo unexpected > review-hook-ran")
    (repo / "first.txt").write_text("real edit\n", encoding="utf-8")
    assert "+real edit" in git_diff(root=repo)["diff"]
    assert "first.txt" in git_status(root=repo)["status"]
    assert not (repo / "review-hook-ran").exists()


def test_native_review_does_not_recurse_into_submodule_helpers(repo: Path) -> None:
    commit = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},nested")
    for review in (git_diff, git_status):
        with pytest.raises(GitError, match="git_submodule_review_required"):
            review(root=repo)
    assert "nested" in git_diff(root=repo, staged=True)["diff"]
    (repo / "first.txt").write_text("scoped change\n", encoding="utf-8")
    assert "+scoped change" in git_diff(root=repo, path="first.txt")["diff"]


def test_registered_diff_tool_forwards_page_and_scope_options(repo: Path) -> None:
    import io
    import json

    from rich.console import Console

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.config import AppConfig
    from alysis_code.session_store import SessionStore

    (repo / "literal[1].txt").write_text("staged text " * 3000, encoding="utf-8")
    _git(repo, "add", "literal[1].txt")
    tools = build_tools(
        root=repo,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=SessionStore(
            enabled=False,
            sessions_dir=repo / "sessions",
            session_id="diff-test",
            cwd=str(repo),
            repo_root=str(repo),
        ),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )
    tool = tools["git_diff"]
    first = git_diff(root=repo, path="literal[1].txt", staged=True)
    args = {
        "path": "literal[1].txt",
        "staged": True,
        "offset": first["next_offset"],
        "diff_id": first["diff_id"],
    }
    result = tool.run(args)
    if isinstance(result, str):
        result = json.loads(result)
    assert result["offset"] == 20000
    assert result["diff"] == git_diff(root=repo, **args)["diff"]
