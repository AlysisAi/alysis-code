from __future__ import annotations

import json
from pathlib import Path

import pytest

from alysis_code.agent.tools_assembly import build_tools
from alysis_code.agent.turn.read_cache import (
    _maybe_reuse_same_batch_read_result,
    _remember_same_batch_read_result,
    _SameBatchReadReuseCache,
)
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools.fs import FsError, fs_read


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "sample.txt").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("options", "content"),
    [
        ({"start_line": 2, "end_line": 3}, "2: two\n3: three\n"),
        ({"end_line": 2}, "1: one\n2: two\n"),
        ({"max_lines": 2}, "1: one\n2: two\n"),
        ({"start_line": 2, "max_lines": 1, "include_line_numbers": False}, "two\n"),
        ({}, "one\ntwo\nthree\nfour\nfive\n"),
    ],
)
def test_reader_supports_raw_and_focused_views(
    workspace: Path, options: dict, content: str
) -> None:
    result = fs_read(root=workspace, path="sample.txt", **options)
    assert result["content"] == content


def test_window_continuation_covers_every_line(workspace: Path) -> None:
    first = fs_read(root=workspace, path="sample.txt", start_line=1, end_line=5, max_lines=2)
    assert first["truncated"] is True
    assert first["next_range"] == {"start_line": 3, "end_line": 4}
    second = fs_read(root=workspace, path="sample.txt", **first["next_range"])
    last = fs_read(root=workspace, path="sample.txt", start_line=5)
    assert first["content"] + second["content"] + last["content"] == (
        "1: one\n2: two\n3: three\n4: four\n5: five\n"
    )


@pytest.mark.parametrize("range_view", [False, True])
def test_clipped_unicode_line_is_bounded_and_not_skipped(tmp_path: Path, range_view: bool) -> None:
    content = "μετρητής" * 100 + "\nafter\n"
    (tmp_path / "unicode.txt").write_text(content, encoding="utf-8")
    result = fs_read(
        root=tmp_path, path="unicode.txt", max_bytes=17, **({"start_line": 1} if range_view else {})
    )
    assert len(result["content"].encode("utf-8")) <= 17
    assert result["line_clipped"] is True
    assert result["next_range"]["start_line"] == 1
    recovered = fs_read(root=tmp_path, path="unicode.txt", max_bytes=4_000, **result["next_range"])
    assert "1: " + content.splitlines()[0] in recovered["content"]


@pytest.mark.parametrize(
    "options", [{"max_bytes": 0}, {"max_bytes": -1}, {"max_lines": 0}, {"end_line": 0}]
)
def test_invalid_limits_fail_instead_of_falling_back(workspace: Path, options: dict) -> None:
    with pytest.raises(FsError):
        fs_read(root=workspace, path="sample.txt", **options)


def test_derived_summary_and_explicit_window_remain_distinct(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("first\n" + "middle\n" * 500 + "last\n")
    summary = fs_read(root=tmp_path, path="package-lock.json")
    window = fs_read(root=tmp_path, path="package-lock.json", start_line=501, end_line=502)
    assert summary["derived_artifact"] is True
    assert window["content"] == "501: middle\n502: last\n"
    assert "derived_artifact" not in window


def test_unified_reader_uses_existing_ledger_for_all_ranges(workspace: Path) -> None:
    store = SessionStore(
        enabled=False,
        sessions_dir=workspace / "sessions",
        session_id="unified",
        cwd=str(workspace),
        repo_root=str(workspace),
    )
    try:
        ledgers = []
        tools = build_tools(
            root=workspace,
            console=None,
            store=store,
            mode="readonly",
            yes=True,
            cfg=AppConfig(model="offline"),
            subagents_enabled=False,
            read_ledger_sink=ledgers.append,
        )
        first = tools["fs_read"].run({"path": "sample.txt", "start_line": 2, "end_line": 3})
        ledgers[0].record_delivery(result=first, content_for_message=json.dumps(first))
        repeat = tools["fs_read"].run({"path": "sample.txt", "start_line": 2, "end_line": 3})
        delta = tools["fs_read"].run({"path": "sample.txt", "start_line": 3, "end_line": 5})
        forced = tools["fs_read"].run(
            {"path": "sample.txt", "start_line": 2, "end_line": 3, "force": True}
        )
        assert first["content"] == forced["content"] == "2: two\n3: three\n"
        assert repeat["read_ledger_skipped"] is True
        assert delta["returned_ranges"] == [{"start_line": 4, "end_line": 5}]
        assert "3: three" not in delta["content"]
        (workspace / "sample.txt").write_text("one\nchanged\nthree\n")
        changed = tools["fs_read"].run({"path": "sample.txt", "start_line": 2, "end_line": 2})
        assert changed["content"] == "2: changed\n"
    finally:
        store.close()


def _remember(cache, root, args, *, result=None):
    if result is None:
        result = fs_read(root=root, **args)
    _remember_same_batch_read_result(
        root=root, cache=cache, tool_name="fs_read", arguments=args, result=result
    )


def _reuse(cache, root, args):
    return _maybe_reuse_same_batch_read_result(
        root=root, cache=cache, tool_name="fs_read", arguments=args
    )


def test_cached_window_never_becomes_whole_file(workspace: Path) -> None:
    cache = _SameBatchReadReuseCache()
    args = {"path": "sample.txt", "start_line": 2, "end_line": 3}
    _remember(cache, workspace, args)
    assert _reuse(cache, workspace, {"path": "sample.txt"}) is None
    assert _reuse(cache, workspace, args)["content"] == "2: two\n3: three\n"
    assert _reuse(cache, workspace, {**args, "force": True}) is None
    assert _reuse(cache, workspace, {**args, "include_line_numbers": False}) is None


def test_cache_keeps_large_explicit_ranges_separate_from_default_byte_limit(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("x" * 20_000 + "\n")
    cache = _SameBatchReadReuseCache()
    args = {"path": "sample.txt", "start_line": 1, "end_line": 1}
    _remember(cache, tmp_path, {**args, "max_bytes": 48_000})
    assert _reuse(cache, tmp_path, args) is None
    larger = _reuse(cache, tmp_path, {**args, "max_bytes": 48_000})
    assert larger is not None
    assert len(larger["content"]) > 12_000


@pytest.mark.parametrize("flag", ["read_ledger_skipped", "read_ledger_partial"])
def test_ledger_notices_cannot_seed_a_reconstructed_file(workspace: Path, flag: str) -> None:
    cache = _SameBatchReadReuseCache()
    args = {"path": "sample.txt"}
    _remember(
        cache,
        workspace,
        args,
        result={"content": "already returned", "truncated": False, flag: True},
    )
    assert _reuse(cache, workspace, args) is None
    assert _reuse(cache, workspace, {**args, "start_line": 1}) is None


def test_cached_head_cannot_hide_window_truncation(workspace: Path) -> None:
    cache = _SameBatchReadReuseCache()
    _remember(cache, workspace, {"path": "sample.txt"})
    args = {"path": "sample.txt", "start_line": 1, "end_line": 5, "max_lines": 2}
    assert _reuse(cache, workspace, args) is None
    real = fs_read(root=workspace, **args)
    assert real["truncated"] is True
    assert real["next_range"] == {"start_line": 3, "end_line": 4}


def test_cache_keeps_relative_path_bases_separate(workspace: Path) -> None:
    cache = _SameBatchReadReuseCache()
    args = {"path": "sample.txt", "start_line": 1, "end_line": 1}
    _remember(cache, workspace, args)
    assert _reuse(cache, workspace, {**args, "path_base": "workspace_root"}) is None
    assert _reuse(cache, workspace, {**args, "path": str(workspace / "sample.txt")}) is None


def test_raw_read_replacement_characters_cannot_expand_past_byte_ceiling(tmp_path: Path) -> None:
    (tmp_path / "invalid.txt").write_bytes(b"\xff" * 8)
    result = fs_read(root=tmp_path, path="invalid.txt", max_bytes=8)
    assert len(result["content"].encode("utf-8")) <= 8
    assert result["truncated"] is True


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x0b", "\x0c", "\x85"])
def test_cached_windows_use_physical_line_boundaries(tmp_path: Path, separator: str) -> None:
    (tmp_path / "sample.txt").write_text(f"one{separator}two\nthree\n", encoding="utf-8")
    cache = _SameBatchReadReuseCache()
    _remember(cache, tmp_path, {"path": "sample.txt"})
    args = {"path": "sample.txt", "start_line": 2, "end_line": 2}
    assert _reuse(cache, tmp_path, args) == fs_read(root=tmp_path, **args)


def test_cache_cannot_bypass_root_escape_error(tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("inside\n")
    cache = _SameBatchReadReuseCache()
    args = {"path": "outside.txt", "path_base": "workspace_root"}
    _remember(cache, tmp_path, args, result=fs_read(root=tmp_path, path="outside.txt"))
    assert _reuse(cache, tmp_path, {"path": "/outside.txt"}) is None


def test_literal_path_punctuation_is_not_removed_for_cache_reuse(tmp_path: Path) -> None:
    cache = _SameBatchReadReuseCache()
    (tmp_path / "quoted.txt").write_text("plain\n")
    (tmp_path / "'quoted.txt'").write_text("quoted\n")
    _remember(cache, tmp_path, {"path": "quoted.txt"})
    assert _reuse(cache, tmp_path, {"path": "'quoted.txt'"}) is None


def test_forced_refresh_supersedes_older_cached_windows(workspace: Path) -> None:
    cache = _SameBatchReadReuseCache()
    args = {"path": "sample.txt", "start_line": 2, "end_line": 2}
    _remember(cache, workspace, args)
    (workspace / "sample.txt").write_text("one\nupdated\nthree\n")
    _remember(
        cache,
        workspace,
        {"path": "sample.txt", "force": True},
        result=fs_read(root=workspace, path="sample.txt"),
    )
    assert _reuse(cache, workspace, args)["content"] == "2: updated\n"


def test_clipped_continuation_stays_within_requested_scope(workspace: Path) -> None:
    result = fs_read(root=workspace, path="sample.txt", start_line=2, end_line=3, max_bytes=8)
    assert result["truncated"] is True
    assert result["next_range"] == {"start_line": 3, "end_line": 3}


def test_failed_forced_refresh_cannot_leave_a_stale_success(workspace: Path) -> None:
    cache = _SameBatchReadReuseCache()
    args = {"path": "sample.txt"}
    _remember(cache, workspace, args)
    (workspace / "sample.txt").rename(workspace / "moved.txt")
    assert _reuse(cache, workspace, {**args, "force": True}) is None
    with pytest.raises(FsError, match="Not found"):
        fs_read(root=workspace, **args)
    assert _reuse(cache, workspace, args) is None
