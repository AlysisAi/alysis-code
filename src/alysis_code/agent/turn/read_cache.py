from __future__ import annotations

import copy
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...tools.fs import fs_read_uses_line_range
from ..tools_assembly import ToolDef

_SAME_BATCH_FS_READ_DEFAULT_MAX_BYTES = 12_000
_SAME_BATCH_READ_WINDOW_DEFAULT_MAX_LINES = 200
_SAME_BATCH_READ_CACHE_SAFE_TOOL_NAMES = {
    "fs_read",
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
class _SameBatchReadWindowRecord:
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
    exact_read_windows: dict[tuple[str, int, int | None, int, bool, int], dict[str, Any]] = field(
        default_factory=dict
    )
    full_fs_reads: dict[str, _SameBatchFsReadRecord] = field(default_factory=dict)
    read_windows_by_path: dict[str, list[_SameBatchReadWindowRecord]] = field(default_factory=dict)

    def clear(self) -> None:
        self.exact_fs_reads.clear()
        self.exact_read_windows.clear()
        self.full_fs_reads.clear()
        self.read_windows_by_path.clear()


def _same_batch_read_path_key(*, root: Path, raw_path: Any, raw_base: Any = None) -> str | None:
    base = str(raw_base or "active_workdir").strip().lower() or "active_workdir"
    if base not in {"active_workdir", "workspace_root"}:
        return None
    text = str(raw_path or "").strip()
    if not text:
        return None
    requested = Path(text)
    if requested.is_absolute():
        base = "workspace_root"
    if base == "workspace_root":
        root_abs = root.resolve()
        try:
            resolved = (root_abs / requested).resolve().relative_to(root_abs)
        except (OSError, RuntimeError, ValueError):
            return None
        return f"workspace_root:{os.fspath(resolved)}"
    # The live active directory is owned by the tool wrapper. Preserve literal
    # path spelling here: prose cleanup or collapsing '..' across a symlink can
    # make different files share a cache entry. Workdir changes invalidate the
    # batch cache through the existing mutation/control-tool boundary.
    return f"active_workdir:{os.fspath(requested)}"


def _split_text_preserving_lines(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    # Match the reader's physical CR/LF/CRLF boundaries, including preserved
    # newlines. str.splitlines also splits Unicode separators within a line.
    return tuple(io.StringIO(text, newline=""))


def _coerce_fs_read_request(
    *,
    root: Path,
    arguments: dict[str, Any],
) -> tuple[str, str, int, bool] | None:
    raw_path = str(arguments.get("path") or "")
    path_key = _same_batch_read_path_key(
        root=root, raw_path=raw_path, raw_base=arguments.get("path_base")
    )
    if not path_key:
        return None
    raw_max_bytes = arguments.get("max_bytes")
    max_bytes = (
        _SAME_BATCH_FS_READ_DEFAULT_MAX_BYTES if raw_max_bytes is None else int(raw_max_bytes)
    )
    if max_bytes < 1:
        return None
    return (
        path_key,
        raw_path,
        max_bytes,
        bool(arguments.get("allow_derived", False)),
    )


def _coerce_read_window_request(
    *,
    root: Path,
    arguments: dict[str, Any],
) -> tuple[str, str, int, int | None, int, bool, int] | None:
    raw_path = str(arguments.get("path") or "")
    path_key = _same_batch_read_path_key(
        root=root, raw_path=raw_path, raw_base=arguments.get("path_base")
    )
    if not path_key:
        return None
    start_line = int(arguments["start_line"]) if arguments.get("start_line") is not None else 1
    requested_end_line = (
        int(arguments["end_line"]) if arguments.get("end_line") is not None else None
    )
    max_lines = (
        int(arguments["max_lines"])
        if arguments.get("max_lines") is not None
        else _SAME_BATCH_READ_WINDOW_DEFAULT_MAX_LINES
    )
    if start_line < 1 or max_lines < 1:
        return None
    if requested_end_line is not None and requested_end_line < start_line:
        return None
    include_line_numbers = (
        bool(arguments["include_line_numbers"])
        if arguments.get("include_line_numbers") is not None
        else True
    )
    raw_max_bytes = arguments.get("max_bytes")
    if raw_max_bytes is None:
        max_bytes = _SAME_BATCH_FS_READ_DEFAULT_MAX_BYTES
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


def _build_read_window_result_from_full_read(
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
    requested_last = (
        total_lines if requested_end_line is None else min(requested_end_line, total_lines)
    )
    if actual_end_line < requested_last:
        # Let the real reader supply its full truncation/continuation metadata.
        return None
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


def _build_read_window_result_from_cached_range(
    *,
    raw_path: str,
    request: tuple[str, str, int, int | None, int, bool, int],
    record: _SameBatchReadWindowRecord,
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

    if requested_end_line is not None and actual_end_line < requested_end_line:
        if record.total_lines is None or actual_end_line < record.total_lines:
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

    if truncated:
        return None

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
    if tool_name != "fs_read":
        return None
    if arguments.get("force") is True:
        # Retire stale snapshots before dispatch: a failed refresh may never
        # reach _remember_same_batch_read_result.
        cache.clear()
        return None
    if not fs_read_uses_line_range(arguments):
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

    request = _coerce_read_window_request(root=root, arguments=arguments)
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
    cached_exact = cache.exact_read_windows.get(
        (path_key, start_line, requested_end_line, max_lines, include_line_numbers, max_bytes)
    )
    if cached_exact is not None:
        reused = copy.deepcopy(cached_exact)
        reused["path"] = raw_path
        return reused

    full_read = cache.full_fs_reads.get(path_key)
    if full_read is not None:
        reused = _build_read_window_result_from_full_read(
            raw_path=raw_path,
            request=request,
            record=full_read,
        )
        if reused is not None:
            return reused

    for record in reversed(cache.read_windows_by_path.get(path_key, [])):
        reused = _build_read_window_result_from_cached_range(
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
    if tool_name != "fs_read":
        return
    if arguments.get("force") is True:
        # A refresh supersedes earlier snapshots, including other ranges and
        # path aliases. This small batch cache can simply discard them all.
        cache.clear()
    if "error" in result or result.get("read_ledger_skipped") or result.get("read_ledger_partial"):
        # A ledger notice/delta is not a file snapshot and must never seed a
        # whole-file or range reconstruction for another call in this batch.
        return
    if not fs_read_uses_line_range(arguments):
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

    request = _coerce_read_window_request(root=root, arguments=arguments)
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
    cache.exact_read_windows[
        (path_key, start_line, requested_end_line, max_lines, include_line_numbers, max_bytes)
    ] = stored
    cache.read_windows_by_path.setdefault(path_key, []).append(
        _SameBatchReadWindowRecord(
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
