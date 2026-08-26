from __future__ import annotations

from pathlib import Path

from alysis_code.agent.turn.read_cache import (
    _maybe_reuse_same_batch_read_result,
    _remember_same_batch_read_result,
    _SameBatchReadReuseCache,
)


def _remember(cache: _SameBatchReadReuseCache, root: Path, **kwargs) -> None:
    _remember_same_batch_read_result(root=root, cache=cache, **kwargs)


def _reuse(cache: _SameBatchReadReuseCache, root: Path, **kwargs):
    return _maybe_reuse_same_batch_read_result(root=root, cache=cache, **kwargs)


def test_fs_read_reuse_key_distinguishes_allow_derived(tmp_path: Path) -> None:
    cache = _SameBatchReadReuseCache()
    stub_result = {
        "path": "package-lock.json",
        "content": "head sample",
        "truncated": True,
        "derived_artifact": True,
    }
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read",
        arguments={"path": "package-lock.json"},
        result=stub_result,
    )

    same_args_hit = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read",
        arguments={"path": "package-lock.json"},
    )
    assert same_args_hit is not None
    assert same_args_hit.get("derived_artifact") is True

    opt_in_miss = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read",
        arguments={"path": "package-lock.json", "allow_derived": True},
    )
    assert opt_in_miss is None


def test_derived_stub_is_never_promoted_to_full_read_cache(tmp_path: Path) -> None:
    cache = _SameBatchReadReuseCache()
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read",
        arguments={"path": "package-lock.json"},
        result={
            "path": "package-lock.json",
            "content": "head only",
            "truncated": True,
            "derived_artifact": True,
        },
    )
    assert cache.full_fs_reads == {}

    line_request = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "package-lock.json", "start_line": 1},
    )
    assert line_request is None


def test_byte_truncated_line_reads_are_not_remembered(tmp_path: Path) -> None:
    cache = _SameBatchReadReuseCache()
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "wide.txt", "start_line": 1},
        result={
            "path": "wide.txt",
            "start_line": 1,
            "end_line": 2,
            "total_lines": None,
            "content": "1: partial",
            "truncated": True,
            "byte_truncated": True,
        },
    )
    assert cache.exact_fs_read_lines == {}
    assert cache.fs_read_lines_by_path == {}


def test_complete_line_reads_are_still_remembered_and_reused(tmp_path: Path) -> None:
    cache = _SameBatchReadReuseCache()
    result = {
        "path": "normal.txt",
        "start_line": 1,
        "end_line": 2,
        "total_lines": 2,
        "content": "1: alpha\n2: beta\n",
        "truncated": False,
    }
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1},
        result=result,
    )
    reused = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1},
    )
    assert reused is not None
    assert reused["content"] == result["content"]


def test_reuse_key_distinguishes_max_bytes(tmp_path: Path) -> None:
    """A small-max_bytes request must never be served a larger cached result."""
    cache = _SameBatchReadReuseCache()
    result = {
        "path": "normal.txt",
        "start_line": 1,
        "end_line": 2,
        "total_lines": 2,
        "content": "1: " + ("a" * 500) + "\n2: " + ("b" * 500) + "\n",
        "truncated": False,
    }
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1, "max_bytes": 2000},
        result=result,
    )

    small_request = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1, "max_bytes": 10},
    )
    assert small_request is None, "10-byte request must not reuse a 1000+-byte cached result"

    same_request = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1, "max_bytes": 2000},
    )
    assert same_request is not None


def test_invalid_max_bytes_is_never_served_from_cache(tmp_path: Path) -> None:
    """max_bytes=0 is invalid at the tool layer (FsError); the cache must miss
    so the real tool raises instead of serving cached default-sized content."""
    cache = _SameBatchReadReuseCache()
    result = {
        "path": "normal.txt",
        "start_line": 1,
        "end_line": 1,
        "total_lines": 1,
        "content": "1: alpha\n",
        "truncated": False,
    }
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1},
        result=result,
    )

    zero = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1, "max_bytes": 0},
    )
    assert zero is None

    negative = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1, "max_bytes": -5},
    )
    assert negative is None

    # And an invalid result is never remembered under a defaulted key either.
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "other.txt", "start_line": 1, "max_bytes": 0},
        result=dict(result, path="other.txt"),
    )
    assert all(key[0] != "other.txt" for key in cache.exact_fs_read_lines)


def test_rebuilt_windows_respect_the_byte_ceiling(tmp_path: Path) -> None:
    """Range rebuilds from cached records honor the request's max_bytes."""
    cache = _SameBatchReadReuseCache()
    _remember(
        cache,
        tmp_path,
        tool_name="fs_read",
        arguments={"path": "normal.txt"},
        result={
            "path": "normal.txt",
            "content": ("a" * 300) + "\n" + ("b" * 300) + "\n",
            "truncated": False,
        },
    )

    capped = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1, "max_bytes": 50},
    )
    assert capped is None, "rebuild larger than max_bytes must fall through to the real tool"

    uncapped = _reuse(
        cache,
        tmp_path,
        tool_name="fs_read_lines",
        arguments={"path": "normal.txt", "start_line": 1},
    )
    assert uncapped is not None
