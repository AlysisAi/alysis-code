from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..prompt_context import _normalize_repo_relative_hint_path
from ..tools_assembly import ToolDef

_SAME_BATCH_FS_READ_DEFAULT_MAX_BYTES = 20_000
_SAME_BATCH_FS_READ_LINES_DEFAULT_MAX_LINES = 200
# Mirrors tools.fs._DEFAULT_READ_LINES_MAX_BYTES: an absent max_bytes argument
# keys and caps identically to the tool's own default ceiling.
_SAME_BATCH_FS_READ_LINES_DEFAULT_MAX_BYTES = 48_000
_SAME_BATCH_READ_CACHE_SAFE_TOOL_NAMES = {
    "fs_read",
    "fs_read_lines",
    "fs_list",
    "web_fetch",
    "web_search",
    "symbol_search",
    "test_discover",
    "repo_map",
    "search_rg",
    "history_search",
    "skill_read",
    "git_status",
    "git_diff",
    "git_history",
}


@dataclass(frozen=True)
class _SameBatchFsReadRecord:
    path_key: str
    raw_lines: tuple[str, ...]
    result: dict[str, Any]


@dataclass(frozen=True)
class _SameBatchFsReadLinesRecord:
    path_key: str
    start_line: int
    end_line: int
    total_lines: int | None
    truncated: bool
    include_line_numbers: bool
    content_lines: tuple[str, ...]
    result: dict[str, Any]


@dataclass
class _SameBatchReadReuseCache:
    exact_fs_reads: dict[tuple[str, int, bool], dict[str, Any]] = field(default_factory=dict)
    exact_fs_read_lines: dict[tuple[str, int, int | None, int, bool, int], dict[str, Any]] = field(
        default_factory=dict
    )
    full_fs_reads: dict[str, _SameBatchFsReadRecord] = field(default_factory=dict)
    fs_read_lines_by_path: dict[str, list[_SameBatchFsReadLinesRecord]] = field(
        default_factory=dict
    )

    def clear(self) -> None:
        self.exact_fs_reads.clear()
        self.exact_fs_read_lines.clear()
        self.full_fs_reads.clear()
        self.fs_read_lines_by_path.clear()


def _same_batch_read_path_key(*, root: Path, raw_path: Any) -> str | None:
    normalized = _normalize_repo_relative_hint_path(root=root, raw=str(raw_path or ""))
    if normalized:
        return normalized
    candidate = str(raw_path or "").strip().replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate:
        return None
    normalized_candidate = os.path.normpath(candidate).replace("\\", "/")
    if normalized_candidate in {"", ".", ".."} or normalized_candidate.startswith("../"):
        return None
    if normalized_candidate.startswith("/"):
        normalized_candidate = normalized_candidate[1:]
    return normalized_candidate or None


def _split_text_preserving_lines(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(text.splitlines(keepends=True))


def _coerce_fs_read_request(
    *,
    root: Path,
    arguments: dict[str, Any],
) -> tuple[str, str, int, bool] | None:
    raw_path = str(arguments.get("path") or "")
    path_key = _same_batch_read_path_key(root=root, raw_path=raw_path)
    if not path_key:
        return None
    raw_max_bytes = arguments.get("max_bytes")
    max_bytes = (
        _SAME_BATCH_FS_READ_DEFAULT_MAX_BYTES if raw_max_bytes is None else int(raw_max_bytes)
    )
    return (
        path_key,
        raw_path,
        max_bytes,
        bool(arguments.get("allow_derived", False)),
    )


def _coerce_fs_read_lines_request(
    *,
    root: Path,
    arguments: dict[str, Any],
) -> tuple[str, str, int, int | None, int, bool, int] | None:
    raw_path = str(arguments.get("path") or "")
    path_key = _same_batch_read_path_key(root=root, raw_path=raw_path)
    if not path_key:
        return None
    start_line = int(arguments["start_line"]) if arguments.get("start_line") is not None else 0
    requested_end_line = (
        int(arguments["end_line"]) if arguments.get("end_line") is not None else None
    )
    max_lines = (
        int(arguments["max_lines"])
        if arguments.get("max_lines") is not None
        else _SAME_BATCH_FS_READ_LINES_DEFAULT_MAX_LINES
    )
    include_line_numbers = bool(arguments.get("include_line_numbers", True))
    raw_max_bytes = arguments.get("max_bytes")
    if raw_max_bytes is None:
        max_bytes = _SAME_BATCH_FS_READ_LINES_DEFAULT_MAX_BYTES
    else:
        max_bytes = int(raw_max_bytes)
        if max_bytes < 1:
            # Invalid ceiling: never serve it from cache - the real tool owns
            # the FsError for out-of-range values.
            return None
    return (
        path_key,
        raw_path,
        start_line,
        requested_end_line,
        max_lines,
        include_line_numbers,
        max_bytes,
    )


def _build_fs_read_lines_result_from_full_fs_read(
    *,
    raw_path: str,
    request: tuple[str, str, int, int | None, int, bool, int],
    record: _SameBatchFsReadRecord,
) -> dict[str, Any] | None:
    _, _, start_line, requested_end_line, max_lines, include_line_numbers, max_bytes = request
    total_lines = len(record.raw_lines)
    if start_line < 1 or start_line > total_lines:
        return None

    effective_end_line = start_line + max_lines - 1
    if requested_end_line is not None:
        effective_end_line = min(effective_end_line, requested_end_line)
    actual_end_line = min(effective_end_line, total_lines)
    selected_lines = record.raw_lines[start_line - 1 : actual_end_line]
    if not selected_lines:
        return None

    if include_line_numbers:
        content = "".join(
            f"{lineno}: {line}"
            for lineno, line in zip(
                range(start_line, actual_end_line + 1), selected_lines, strict=False
            )
        )
    else:
        content = "".join(selected_lines)

    if len(content.encode("utf-8")) > max_bytes:
        # The rebuilt window would exceed the request's byte ceiling; let the
        # real tool run and apply its own clipping rather than replicate it.
        return None

    return {
        "path": raw_path,
        "start_line": start_line,
        "end_line": actual_end_line,
        "total_lines": total_lines if actual_end_line == total_lines else None,
        "content": content,
        "truncated": requested_end_line is None and actual_end_line < total_lines,
    }


def _build_fs_read_lines_result_from_cached_range(
    *,
    raw_path: str,
    request: tuple[str, str, int, int | None, int, bool, int],
    record: _SameBatchFsReadLinesRecord,
) -> dict[str, Any] | None:
    _, _, start_line, requested_end_line, max_lines, include_line_numbers, max_bytes = request
    if start_line < record.start_line:
        return None
    if include_line_numbers != record.include_line_numbers:
        return None

    requested_max_end = start_line + max_lines - 1
    if requested_end_line is not None:
        requested_max_end = min(requested_max_end, requested_end_line)

    if requested_end_line is not None:
        if requested_max_end <= record.end_line:
            actual_end_line = requested_max_end
        elif (
            record.total_lines is not None
            and start_line <= record.total_lines
            and record.end_line == record.total_lines
        ):
            actual_end_line = record.total_lines
        else:
            return None
    else:
        if requested_max_end < record.end_line:
            actual_end_line = requested_max_end
        elif requested_max_end == record.end_line:
            if record.total_lines is not None:
                actual_end_line = requested_max_end
            elif record.truncated:
                actual_end_line = requested_max_end
            else:
                return None
        elif (
            record.total_lines is not None
            and start_line <= record.total_lines
            and record.end_line == record.total_lines
        ):
            actual_end_line = record.total_lines
        else:
            return None

    if actual_end_line < start_line:
        return None

    start_offset = start_line - record.start_line
    end_offset = actual_end_line - record.start_line + 1
    selected_lines = record.content_lines[start_offset:end_offset]
    if len(selected_lines) != (actual_end_line - start_line + 1):
        return None

    truncated = False
    if requested_end_line is None:
        if actual_end_line < record.end_line:
            truncated = True
        elif record.total_lines is not None:
            truncated = actual_end_line < record.total_lines
        else:
            truncated = record.truncated

    content = "".join(selected_lines)
    if len(content.encode("utf-8")) > max_bytes:
        # The rebuilt window would exceed the request's byte ceiling; let the
        # real tool run and apply its own clipping rather than replicate it.
        return None

    return {
        "path": raw_path,
        "start_line": start_line,
        "end_line": actual_end_line,
        "total_lines": (
            record.total_lines
            if record.total_lines is not None and actual_end_line == record.total_lines
            else None
        ),
        "content": content,
        "truncated": truncated,
    }


def _maybe_reuse_same_batch_read_result(
    *,
    root: Path,
    cache: _SameBatchReadReuseCache,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_name == "fs_read":
        request = _coerce_fs_read_request(root=root, arguments=arguments)
        if request is None:
            return None
        path_key, raw_path, max_bytes, allow_derived = request
        cached = cache.exact_fs_reads.get((path_key, max_bytes, allow_derived))
        if cached is None:
            return None
        reused = copy.deepcopy(cached)
        reused["path"] = raw_path
        return reused

    if tool_name != "fs_read_lines":
        return None

    request = _coerce_fs_read_lines_request(root=root, arguments=arguments)
    if request is None:
        return None
    (
        path_key,
        raw_path,
        start_line,
        requested_end_line,
        max_lines,
        include_line_numbers,
        max_bytes,
    ) = request
    cached_exact = cache.exact_fs_read_lines.get(
        (path_key, start_line, requested_end_line, max_lines, include_line_numbers, max_bytes)
    )
    if cached_exact is not None:
        reused = copy.deepcopy(cached_exact)
        reused["path"] = raw_path
        return reused

    full_read = cache.full_fs_reads.get(path_key)
    if full_read is not None:
        reused = _build_fs_read_lines_result_from_full_fs_read(
            raw_path=raw_path,
            request=request,
            record=full_read,
        )
        if reused is not None:
            return reused

    for record in reversed(cache.fs_read_lines_by_path.get(path_key, [])):
        reused = _build_fs_read_lines_result_from_cached_range(
            raw_path=raw_path,
            request=request,
            record=record,
        )
        if reused is not None:
            return reused
    return None


def _remember_same_batch_read_result(
    *,
    root: Path,
    cache: _SameBatchReadReuseCache,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if tool_name == "fs_read":
        request = _coerce_fs_read_request(root=root, arguments=arguments)
        if request is None:
            return
        path_key, _, max_bytes, allow_derived = request
        stored = copy.deepcopy(result)
        cache.exact_fs_reads[(path_key, max_bytes, allow_derived)] = stored
        if bool(result.get("truncated")):
            return
        content = result.get("content")
        if isinstance(content, str):
            cache.full_fs_reads[path_key] = _SameBatchFsReadRecord(
                path_key=path_key,
                raw_lines=_split_text_preserving_lines(content),
                result=stored,
            )
        return

    if tool_name != "fs_read_lines":
        return
    request = _coerce_fs_read_lines_request(root=root, arguments=arguments)
    if request is None:
        return
    (
        path_key,
        _,
        start_line,
        requested_end_line,
        max_lines,
        include_line_numbers,
        max_bytes,
    ) = request
    end_line = result.get("end_line")
    content = result.get("content")
    if not isinstance(end_line, int) or not isinstance(content, str):
        return
    if bool(result.get("byte_truncated")) or bool(result.get("line_clipped")):
        # A byte-capped window may end mid-line; reusing it to answer other
        # range requests would silently serve incomplete lines.
        return
    stored = copy.deepcopy(result)
    cache.exact_fs_read_lines[
        (path_key, start_line, requested_end_line, max_lines, include_line_numbers, max_bytes)
    ] = stored
    cache.fs_read_lines_by_path.setdefault(path_key, []).append(
        _SameBatchFsReadLinesRecord(
            path_key=path_key,
            start_line=start_line,
            end_line=end_line,
            total_lines=(
                int(result["total_lines"]) if result.get("total_lines") is not None else None
            ),
            truncated=bool(result.get("truncated")),
            include_line_numbers=include_line_numbers,
            content_lines=_split_text_preserving_lines(content),
            result=stored,
        )
    )


def _same_batch_read_cache_should_invalidate(tool_name: str, tool: ToolDef | None) -> bool:
    normalized = str(tool_name or "").strip()
    if not normalized:
        return True
    if normalized not in _SAME_BATCH_READ_CACHE_SAFE_TOOL_NAMES:
        return True
    return tool is None
