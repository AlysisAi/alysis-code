from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LineRange = tuple[int, int]


@dataclass
class _FileReads:
    content_sha256: str
    ranges: list[LineRange]


class SessionReadLedger:
    """Suppress unchanged file ranges already returned within one agent session."""

    def __init__(self, *, root: Path, enabled: bool = True) -> None:
        self.root = root.resolve()
        self.enabled = bool(enabled)
        self._files: dict[str, _FileReads] = {}
        self._lock = threading.Lock()

    def content_hash(self, path: str) -> str | None:
        if not self.enabled:
            return None
        path_obj = (self.root / path).resolve()
        try:
            path_obj.relative_to(self.root)
        except ValueError:
            return None
        try:
            digest = hashlib.sha256()
            with path_obj.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except (FileNotFoundError, IsADirectoryError, OSError):
            return None
        return digest.hexdigest()

    def invalidate(self, *paths: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            for path in paths:
                self._files.pop(str(path), None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return an immutable-by-convention copy for session continuation."""
        if not self.enabled:
            return {}
        with self._lock:
            return {
                path: {
                    "content_sha256": entry.content_sha256,
                    "ranges": list(entry.ranges),
                }
                for path, entry in self._files.items()
            }

    def seed_from_snapshot(self, snapshot: dict[str, dict[str, Any]]) -> int:
        """Import still-valid ranges from an earlier incarnation of this session."""
        if not self.enabled:
            return 0
        imported: dict[str, _FileReads] = {}
        for path, raw_entry in snapshot.items():
            if not isinstance(path, str) or not isinstance(raw_entry, dict):
                continue
            expected_hash = str(raw_entry.get("content_sha256") or "")
            if not expected_hash or self.content_hash(path) != expected_hash:
                continue
            raw_ranges = raw_entry.get("ranges")
            if not isinstance(raw_ranges, list):
                continue
            ranges = [
                (int(item[0]), int(item[1]))
                for item in raw_ranges
                if isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], int)
                and isinstance(item[1], int)
                and item[0] > 0
                and item[1] >= item[0]
            ]
            if ranges:
                imported[path] = _FileReads(
                    content_sha256=expected_hash,
                    ranges=_merge_ranges(ranges),
                )
        with self._lock:
            self._files.update(imported)
        return len(imported)

    def filter_result(
        self,
        *,
        path: str,
        result: dict[str, Any],
        content_hash_before: str | None,
        force: bool,
    ) -> dict[str, Any]:
        if not self.enabled:
            return result
        content_hash_after = self.content_hash(path)
        if (
            content_hash_before is None
            or content_hash_after is None
            or content_hash_before != content_hash_after
        ):
            self.invalidate(path)
            return result

        returned_range = _returned_line_range(result)
        if returned_range is None:
            return result

        with self._lock:
            entry = self._files.get(path)
            if entry is None or entry.content_sha256 != content_hash_after:
                entry = _FileReads(content_sha256=content_hash_after, ranges=[])
                self._files[path] = entry
            previous_ranges = list(entry.ranges)
            if force:
                entry.ranges = _merge_ranges([*entry.ranges, returned_range])
                return {**result, "read_ledger_forced": True}

            unread_ranges = _subtract_ranges(returned_range, previous_ranges)
            if unread_ranges == [returned_range]:
                entry.ranges = _merge_ranges([*entry.ranges, returned_range])
                return result

            skipped_ranges = _subtract_ranges(returned_range, unread_ranges)
            notice = _notice(
                path=path,
                skipped_ranges=skipped_ranges,
                previous_ranges=previous_ranges,
            )
            entry.ranges = _merge_ranges([*entry.ranges, returned_range])

        if not unread_ranges:
            return {
                **result,
                "content": notice,
                "read_ledger_skipped": True,
                "read_ledger_notice": notice,
                "returned_ranges": [],
                "skipped_ranges": _range_payloads(skipped_ranges),
            }

        unread_content = _slice_content(
            content=str(result.get("content") or ""),
            returned_start=returned_range[0],
            unread_ranges=unread_ranges,
        )
        separator = "" if not unread_content or unread_content.endswith("\n") else "\n"
        return {
            **result,
            "content": f"{unread_content}{separator}{notice}",
            "read_ledger_partial": True,
            "read_ledger_notice": notice,
            "returned_ranges": _range_payloads(unread_ranges),
            "skipped_ranges": _range_payloads(skipped_ranges),
        }


def _returned_line_range(result: dict[str, Any]) -> LineRange | None:
    returned = result.get("returned_range")
    if isinstance(returned, dict):
        start = returned.get("start_line")
        end = returned.get("end_line")
    elif isinstance(result.get("start_line"), int) and isinstance(result.get("end_line"), int):
        start = result["start_line"]
        end = result["end_line"]
    else:
        content = str(result.get("content") or "")
        line_count = content.count("\n")
        if content and not content.endswith("\n"):
            line_count += 1
        start = 1
        end = line_count
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if result.get("line_clipped") is True:
        end -= 1
    if end < start:
        return None
    return start, end


def _merge_ranges(ranges: list[LineRange]) -> list[LineRange]:
    merged: list[LineRange] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _subtract_ranges(target: LineRange, covered: list[LineRange]) -> list[LineRange]:
    remaining = [target]
    for covered_start, covered_end in _merge_ranges(covered):
        next_remaining: list[LineRange] = []
        for start, end in remaining:
            if covered_end < start or covered_start > end:
                next_remaining.append((start, end))
                continue
            if start < covered_start:
                next_remaining.append((start, covered_start - 1))
            if covered_end < end:
                next_remaining.append((covered_end + 1, end))
        remaining = next_remaining
    return remaining


def _range_text(ranges: list[LineRange]) -> str:
    return ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


def _range_payloads(ranges: list[LineRange]) -> list[dict[str, int]]:
    return [{"start_line": start, "end_line": end} for start, end in ranges]


def _notice(
    *,
    path: str,
    skipped_ranges: list[LineRange],
    previous_ranges: list[LineRange],
) -> str:
    skipped = _range_text(skipped_ranges)
    previous = _range_text(_merge_ranges(previous_ranges))
    return (
        f"lines {skipped} of {path} were already returned in this session (unchanged); "
        f"re-read skipped; request outside {previous} or set force=true"
    )


def _slice_content(
    *,
    content: str,
    returned_start: int,
    unread_ranges: list[LineRange],
) -> str:
    lines = content.splitlines(keepends=True)
    pieces: list[str] = []
    for start, end in unread_ranges:
        first = max(0, start - returned_start)
        last = max(first, end - returned_start + 1)
        pieces.extend(lines[first:last])
    return "".join(pieces)
