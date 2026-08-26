from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from alysis_code.code_review import (
    ChatReviewerClient,
    CodeReviewEngine,
    GitReviewError,
    InvalidReviewRequest,
    ReviewLimits,
    ReviewRequest,
    ReviewResponseError,
)
from alysis_code.llm.types import LLMError, LLMResponse, UsageContract


class FakeReviewer:
    def __init__(self, responses: list[Mapping[str, Any] | str] | None = None) -> None:
        self.responses = responses or [
            {"verdict": "approve", "overview": "No defects found.", "findings": []}
        ]
        self.calls: list[dict[str, Any]] = []

    def review(self, **kwargs: Any) -> Mapping[str, Any] | str:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.test")
    _git(repo, "config", "user.name", "Review Test")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "lib.py").write_text("ENABLED = False\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _finding(path: str, *, secret: str = "") -> dict[str, Any]:
    return {
        "severity": "high",
        "title": "Wrong return value",
        "explanation": f"The changed function returns the wrong value. {secret}",
        "path": path,
        "line_start": 2,
        "line_end": 2,
        "evidence": "The added line returns 2.",
        "suggested_fix": "Return the required value or update callers.",
        "confidence": "high",
    }


def test_working_tree_reviews_staged_unstaged_and_untracked_with_secret_exclusions(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    (repo / "lib.py").write_text("ENABLED = True\n", encoding="utf-8")
    (repo / "new.py").write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n"
        "AWS_ID=AKIAABCDEFGHIJKLMNOP\n"
        "KEY='-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----'\n"
        "print('new')\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text("PASSWORD=do-not-send-this\n", encoding="utf-8")

    reviewer = FakeReviewer(
        [
            {
                "verdict": "request_changes",
                "overview": "One blocking correctness defect.",
                "findings": [_finding("app.py")],
            }
        ]
    )
    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())

    assert result.diff.changed_files == ("app.py", "lib.py", ".env", "new.py")
    assert result.diff.included_files == ("app.py", "lib.py", "new.py")
    assert any(
        item.path == ".env" and item.reason == "sensitive_path"
        for item in result.diff.omitted_files
    )
    assert "do-not-send-this" not in result.diff.patch
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.diff.patch
    assert "AKIAABCDEFGHIJKLMNOP" not in result.diff.patch
    assert "BEGIN PRIVATE KEY" not in result.diff.patch
    assert "<redacted>" in result.diff.patch
    assert "### staged: app.py" in result.diff.patch
    assert "### unstaged: lib.py" in result.diff.patch
    assert "### untracked: new.py" in result.diff.patch
    assert result.findings[0].line_start == result.findings[0].line_end == 2
    assert result.summary.finding_counts["high"] == 1
    assert "do-not-send-this" not in reviewer.calls[0]["user_prompt"]


def test_review_omits_private_key_and_credential_directory_canaries(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / ".kube").mkdir()
    (repo / ".kube" / "config").write_text("kube-review-canary\n", encoding="utf-8")
    (repo / "keys").mkdir()
    (repo / "keys" / "deploy.ppk").write_text("ppk-review-canary\n", encoding="utf-8")
    reviewer = FakeReviewer()

    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())

    omitted = {(item.path, item.reason) for item in result.diff.omitted_files}
    assert (".kube/config", "sensitive_path") in omitted
    assert ("keys/deploy.ppk", "sensitive_path") in omitted
    assert "kube-review-canary" not in result.diff.patch
    assert "ppk-review-canary" not in result.diff.patch
    assert "kube-review-canary" not in reviewer.calls[0]["user_prompt"]
    assert "ppk-review-canary" not in reviewer.calls[0]["user_prompt"]


def test_branch_scope_uses_merge_base_and_only_reviews_branch_delta(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "feature")

    reviewer = FakeReviewer()
    result = CodeReviewEngine(repo, reviewer).review(
        ReviewRequest.branch(base="main", head="feature")
    )

    assert result.diff.changed_files == ("feature.py",)
    assert result.diff.included_files == ("feature.py",)
    assert result.diff.metadata["base"] == _git(repo, "rev-parse", "main")
    assert result.diff.metadata["head"] == _git(repo, "rev-parse", "feature")
    assert result.diff.metadata["merge_base"] == _git(repo, "merge-base", "main", "feature")
    assert "FEATURE = True" in result.diff.patch


@pytest.mark.parametrize("scope", ["commit", "range"])
def test_explicit_commit_and_range_scopes(tmp_path: Path, scope: str) -> None:
    repo = _init_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("def value():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "change")
    head = _git(repo, "rev-parse", "HEAD")
    request = (
        ReviewRequest.commit(head)
        if scope == "commit"
        else ReviewRequest.revision_range(base=base, head=head)
    )

    result = CodeReviewEngine(repo, FakeReviewer()).review(request)

    assert result.diff.changed_files == ("app.py",)
    assert "+    return 3" in result.diff.patch


def test_root_commit_scope_is_supported(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    root_commit = _git(repo, "rev-list", "--max-parents=0", "HEAD")

    result = CodeReviewEngine(repo, FakeReviewer()).review(ReviewRequest.commit(root_commit))

    assert set(result.diff.changed_files) == {"app.py", "lib.py"}
    assert "new file mode" in result.diff.patch


@pytest.mark.parametrize(
    "revision",
    ["-p", "HEAD..main", "main;echo-pwned", "main@{1}", "feature branch", "main\\evil"],
)
def test_unsafe_revisions_are_rejected_before_git_diff(tmp_path: Path, revision: str) -> None:
    repo = _init_repo(tmp_path)
    engine = CodeReviewEngine(repo, FakeReviewer())

    with pytest.raises(InvalidReviewRequest, match="unsafe"):
        engine.collect_diff(ReviewRequest.commit(revision))


def test_caps_are_enforced_and_truncation_is_surfaced(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "large.py").write_text("VALUE = 'abcdefghij'\n" * 200, encoding="utf-8")
    (repo / "later.py").write_text("LATER = True\n", encoding="utf-8")
    limits = ReviewLimits(max_files=1, max_file_bytes=512, max_total_bytes=700)

    result = CodeReviewEngine(repo, FakeReviewer(), limits=limits).review(
        ReviewRequest.working_tree()
    )

    assert result.diff.truncated is True
    assert result.summary.truncated is True
    assert result.summary.verdict == "comment"
    assert "review was incomplete" in result.summary.overview
    assert len(result.diff.patch.encode("utf-8")) <= limits.max_total_bytes
    assert any(item.reason == "file_count_limit" for item in result.diff.omitted_files)
    assert "one_or_more_file_diffs_truncated" in result.diff.warnings


def test_empty_working_tree_does_not_call_reviewer(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    reviewer = FakeReviewer()

    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())

    assert result.summary.verdict == "approve"
    assert result.summary.reviewed_file_count == 0
    assert reviewer.calls == []


def test_only_sensitive_changes_are_not_sent_and_do_not_report_approval(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".env").write_text("PASSWORD=do-not-send-this\n", encoding="utf-8")
    reviewer = FakeReviewer()

    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())

    assert result.summary.verdict == "comment"
    assert result.summary.changed_file_count == 1
    assert result.summary.reviewed_file_count == 0
    assert reviewer.calls == []


def test_rename_from_sensitive_path_excludes_the_whole_rename(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".env").write_text("PASSWORD=do-not-send-this\n", encoding="utf-8")
    _git(repo, "add", ".env")
    _git(repo, "commit", "-q", "-m", "add sensitive file")
    _git(repo, "mv", ".env", "settings.txt")
    reviewer = FakeReviewer()

    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())

    assert result.diff.patch == ""
    assert "do-not-send-this" not in json.dumps(result.to_dict())
    assert any(item.reason == "sensitive_path" for item in result.diff.omitted_files)
    assert reviewer.calls == []


def test_secret_shaped_filename_is_redacted_and_never_reviewed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    secret_path = "api_token=sk-abcdefghijklmnopqrstuvwxyz"
    (repo / secret_path).write_text("not relevant\n", encoding="utf-8")
    reviewer = FakeReviewer()

    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())
    encoded = json.dumps(result.to_dict())

    assert secret_path not in encoded
    assert result.diff.changed_files == ("<redacted path>",)
    assert result.diff.omitted_files[0].path == "<redacted path>"
    assert reviewer.calls == []


def test_binary_tracked_and_untracked_files_are_not_sent_to_the_reviewer(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "tracked.bin").write_bytes(b"before\0bytes")
    _git(repo, "add", "tracked.bin")
    _git(repo, "commit", "-q", "-m", "add binary")
    (repo / "tracked.bin").write_bytes(b"after\0bytes")
    (repo / "untracked.bin").write_bytes(bytes(range(1, 20)))
    reviewer = FakeReviewer()

    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())

    assert result.diff.patch == ""
    assert result.summary.verdict == "comment"
    assert {item.path for item in result.diff.omitted_files} == {
        "tracked.bin",
        "untracked.bin",
    }
    assert all(item.reason == "binary_file" for item in result.diff.omitted_files)
    assert reviewer.calls == []


def test_invalid_model_response_retries_then_fails_closed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    invalid = {
        "verdict": "request_changes",
        "overview": "Bad path.",
        "findings": [_finding("outside.py")],
    }
    reviewer = FakeReviewer([invalid, invalid])

    with pytest.raises(ReviewResponseError, match="not a reviewed file"):
        CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())

    assert len(reviewer.calls) == 2
    assert "previous response was invalid" in reviewer.calls[1]["user_prompt"]


def test_model_output_is_redacted_before_result_is_returned(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    reviewer = FakeReviewer(
        [
            {
                "verdict": "request_changes",
                "overview": f"Found secret {secret}",
                "findings": [_finding("app.py", secret=secret)],
            }
        ]
    )

    result = CodeReviewEngine(repo, reviewer).review(ReviewRequest.working_tree())
    encoded = json.dumps(result.to_dict())

    assert secret not in encoded
    assert "<redacted>" in encoded


def test_workspace_must_be_repository_root(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    child = repo / "src"
    child.mkdir()

    with pytest.raises(InvalidReviewRequest, match="repository root"):
        CodeReviewEngine(child, FakeReviewer())


def test_untracked_symlink_is_never_opened(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("PASSWORD=outside-secret-value\n", encoding="utf-8")
    link = repo / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = CodeReviewEngine(repo, FakeReviewer()).review(ReviewRequest.working_tree())

    assert result.diff.patch == ""
    assert any(item.reason == "symlink_not_reviewed" for item in result.diff.omitted_files)
    assert "outside-secret-value" not in json.dumps(result.to_dict())


def test_git_timeout_surfaces_a_bounded_public_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    engine = CodeReviewEngine(repo, FakeReviewer())

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise GitReviewError("git command timed out")

    monkeypatch.setattr(engine, "_run_git", timeout)
    with pytest.raises(GitReviewError, match="timed out"):
        engine.collect_diff(ReviewRequest.working_tree())


class FakeChatClient:
    base_url = "https://example.test"
    model = "test-model"
    supports_tool_calling = False
    supports_forced_tool_choice = False
    usage_contract = UsageContract()

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.kwargs = kwargs
        return LLMResponse(
            content='{"verdict":"approve","overview":"Clean.","findings":[]}',
            tool_calls=[],
            raw={},
        )


def test_chat_reviewer_adapter_uses_existing_chat_client_contract() -> None:
    chat_client = FakeChatClient()
    adapter = ChatReviewerClient(chat_client)  # type: ignore[arg-type]

    raw = adapter.review(
        system_prompt="system", user_prompt="user", response_schema={"type": "object"}
    )

    assert isinstance(raw, str)
    assert chat_client.kwargs["temperature"] == 0.0
    assert chat_client.kwargs["stream"] is False
    assert chat_client.kwargs["response_format"]["type"] == "json_schema"
    assert chat_client.kwargs["messages"][0] == {"role": "system", "content": "system"}


class FallbackChatClient(FakeChatClient):
    def __init__(self) -> None:
        super().__init__()
        self.formats: list[dict[str, Any] | None] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        response_format = kwargs.get("response_format")
        self.formats.append(response_format)
        if response_format is not None:
            raise LLMError("Anthropic Messages does not support response_format")
        return super().chat(**kwargs)


def test_chat_reviewer_adapter_falls_back_when_structured_formats_are_unsupported() -> None:
    chat_client = FallbackChatClient()
    adapter = ChatReviewerClient(chat_client)  # type: ignore[arg-type]

    raw = adapter.review(system_prompt="system", user_prompt="user", response_schema={})

    assert isinstance(raw, str)
    assert [item and item["type"] for item in chat_client.formats] == [
        "json_schema",
        "json_object",
        None,
    ]
