from __future__ import annotations

import json
import os
import threading
from collections import Counter
from typing import Any

from ..forge import RunPaths, now_iso


class AssetUsageLogger:
    def __init__(self, *, run_paths: RunPaths, task_id: str) -> None:
        self.run_paths = run_paths
        self.task_id = task_id
        self.path = run_paths.execution_asset_usage_dir / f"{_safe_task(task_id)}.jsonl"
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def event(self, event: str, **payload: Any) -> None:
        clean_event = str(event or "").strip()
        if not clean_event:
            return
        body = {
            "event": clean_event,
            "ts": now_iso(),
            **_sanitize_payload(payload),
        }
        self._append_json_line(body)
        self._counts[clean_event] += 1

    def mirror(self, *, asset_id: str, kind: str, status: str) -> None:
        self.event("mirror", asset_id=asset_id, kind=kind, status=status)

    def allocation_decision(self, *, asset_id: str, mode: str) -> None:
        self.event("allocation_decision", asset_id=asset_id, mode=mode)

    def inline_injection(self, *, asset_id: str, kind: str) -> None:
        self.event("inline_injection", asset_id=asset_id, kind=kind)

    def asset_read(self, *, asset_id: str, focus: bool, chars: int, cached: bool) -> None:
        self.event(
            "asset_read",
            asset_id=asset_id,
            focus=bool(focus),
            chars=max(0, int(chars)),
            cached=bool(cached),
        )

    def asset_load(self, *, asset_id: str, kind: str, chars: int) -> None:
        self.event("asset_load", asset_id=asset_id, kind=kind, chars=max(0, int(chars)))

    def summary(self, *, primary_count: int, may_need_count: int, pinned_count: int) -> None:
        self.event(
            "summary",
            primary_count=max(0, int(primary_count)),
            may_need_count=max(0, int(may_need_count)),
            pinned_count=max(0, int(pinned_count)),
            reads=int(self._counts.get("asset_read", 0)),
            loads=int(self._counts.get("asset_load", 0)),
        )

    def _append_json_line(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        encoded = line.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(fd, encoded)
            finally:
                os.close(fd)


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        clean_key = str(key or "").strip()
        if not clean_key:
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            sanitized[clean_key] = value
        else:
            sanitized[clean_key] = str(value)
    return sanitized


def _safe_task(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_id)
    return safe or "task"
