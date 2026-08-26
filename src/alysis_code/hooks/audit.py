from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..error_text import sanitize_error_summary
from ..session_store import sanitize_session_id
from .models import HookInvocationContext

HOOK_AUDIT_ARTIFACT_PARTS: tuple[str, str] = ("hooks", "hook_runs.jsonl")
_MAX_WARNING_PREVIEW_ITEMS = 3
_MAX_WARNING_PREVIEW_CHARS = 160


def _now_ts() -> str:
    return datetime.now(UTC).isoformat()


def _truncate_preview(text: str, *, max_chars: int = _MAX_WARNING_PREVIEW_CHARS) -> str:
    return sanitize_error_summary(str(text or ""), max_chars=max_chars)


def hook_audit_log_path(*, sessions_dir: Path, session_id: str) -> Path:
    return (
        sessions_dir
        / sanitize_session_id(session_id)
        / HOOK_AUDIT_ARTIFACT_PARTS[0]
        / HOOK_AUDIT_ARTIFACT_PARTS[1]
    )


def hook_audit_artifact_path(*, sessions_dir: Path, session_id: str) -> Path:
    return hook_audit_log_path(sessions_dir=sessions_dir, session_id=session_id)


def _redacted_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    warning_items = payload.get("warnings")
    warning_list = [str(item) for item in warning_items] if isinstance(warning_items, list) else []
    return {
        "event_name": str(payload.get("event_name") or ""),
        "source_path": str(payload.get("source_path") or ""),
        "source_scope": str(payload.get("source_scope") or ""),
        "matcher": str(payload.get("matcher") or ""),
        "hook_id": str(payload.get("hook_id") or ""),
        "priority": int(payload.get("priority") or 0),
        "failure_policy": str(payload.get("failure_policy") or ""),
        "command_preview": _truncate_preview(
            str(payload.get("command") or ""),
            max_chars=180,
        ),
        "timeout_s": float(payload.get("timeout_s") or 0.0),
        "trusted": bool(payload.get("trusted")),
        "returncode": payload.get("returncode"),
        "blocked": bool(payload.get("blocked")),
        "modified_input": bool(payload.get("modified_input")),
        "modified_input_fields": [
            str(item)
            for item in (payload.get("modified_input_fields") or [])
            if str(item or "").strip()
        ],
        "modified_prompt": bool(payload.get("modified_prompt")),
        "modified_prompt_chars": int(payload.get("modified_prompt_chars") or 0),
        "additional_system_message_count": int(payload.get("additional_system_message_count") or 0),
        "additional_user_message_count": int(payload.get("additional_user_message_count") or 0),
        "system_notices_count": int(payload.get("system_notices_count") or 0),
        "halt_requested": bool(payload.get("halt_requested")),
        "allow_short_circuited": bool(payload.get("allow_short_circuited")),
        "suppress_output": bool(payload.get("suppress_output")),
        "permission_decision": str(payload.get("permission_decision") or ""),
        "stop_reason_preview": _truncate_preview(
            str(payload.get("stop_reason") or ""),
            max_chars=180,
        ),
        "stdout_chars": int(payload.get("stdout_chars") or 0),
        "stderr_chars": int(payload.get("stderr_chars") or 0),
        "duration_ms": int(payload.get("duration_ms") or 0),
        "status": str(payload.get("status") or ""),
        "warning_count": len(warning_list),
        "warnings_preview": [
            _truncate_preview(item) for item in warning_list[:_MAX_WARNING_PREVIEW_ITEMS]
        ],
        "payload_truncated": bool(payload.get("payload_truncated")),
        "payload_bytes": int(payload.get("payload_bytes") or 0),
    }


class HookAuditLogger:
    def __init__(self, *, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id

    def append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")

    def close(self) -> None:
        return None


def read_hook_audit_events(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def build_hook_audit_event(
    *,
    session_id: str,
    context: HookInvocationContext,
) -> dict[str, Any]:
    payload = {
        "event_name": context.event_name,
        "source_path": context.source_path,
        "source_scope": context.source_scope,
        "matcher": context.matcher,
        "hook_id": context.hook_id,
        "priority": context.priority,
        "failure_policy": context.failure_policy,
        "command": context.command,
        "timeout_s": context.timeout_s,
        "trusted": context.trusted,
        "returncode": context.returncode,
        "blocked": context.blocked,
        "modified_input": context.modified_input,
        "modified_input_fields": list(context.modified_input_fields),
        "modified_prompt": context.modified_prompt,
        "modified_prompt_chars": context.modified_prompt_chars,
        "additional_system_message_count": context.additional_system_message_count,
        "additional_user_message_count": context.additional_user_message_count,
        "system_notices_count": context.system_notices_count,
        "halt_requested": context.halt_requested,
        "allow_short_circuited": context.allow_short_circuited,
        "suppress_output": context.suppress_output,
        "permission_decision": context.permission_decision,
        "stop_reason": context.stop_reason,
        "stdout_chars": context.stdout_chars,
        "stderr_chars": context.stderr_chars,
        "duration_ms": context.duration_ms,
        "status": context.status,
        "warnings": list(context.warnings),
        "payload_truncated": context.payload_truncated,
        "payload_bytes": context.payload_bytes,
    }
    redacted = _redacted_hook_payload(payload)
    redacted["type"] = "hook_command"
    redacted["ts"] = _now_ts()
    redacted["session_id"] = session_id
    return redacted


__all__ = [
    "HOOK_AUDIT_ARTIFACT_PARTS",
    "HookAuditLogger",
    "build_hook_audit_event",
    "hook_audit_artifact_path",
    "hook_audit_log_path",
    "read_hook_audit_events",
]
