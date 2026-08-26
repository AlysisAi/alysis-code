from __future__ import annotations

import json
from typing import Any

from ...surface.base import Surface


def _legacy_message_tool_events_required(surface: Surface | object) -> bool:
    """Whether a surface still needs the legacy half of dual event delivery."""
    return not bool(getattr(surface, "canonical_message_tool_events", False))


def _event_preview(value: Any, *, max_chars: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _emit_message_delta_event(
    surface: Surface | object,
    text: str,
    *,
    worker_id: str | None = None,
    role: str | None = None,
) -> None:
    if not text:
        return
    handler = getattr(surface, "emit_message_delta", None)
    if callable(handler):
        handler(text, worker_id=worker_id, role=role)


def _emit_message_end_event(
    surface: Surface | object,
    text: str,
    *,
    worker_id: str | None = None,
    role: str | None = None,
) -> None:
    handler = getattr(surface, "emit_message_end", None)
    if callable(handler):
        handler(text, worker_id=worker_id, role=role)


def _emit_assistant_message_events(
    surface: Surface | object,
    text: str,
    *,
    streamed_text_emitted: bool = False,
    worker_id: str | None = None,
    role: str | None = None,
) -> None:
    if not streamed_text_emitted:
        _emit_message_delta_event(surface, text, worker_id=worker_id, role=role)
    _emit_message_end_event(surface, text, worker_id=worker_id, role=role)


def _emit_tool_call_started_event(
    surface: Surface | object,
    *,
    call_id: str,
    name: str,
    arguments: Any,
    worker_id: str | None = None,
    role: str | None = None,
) -> None:
    handler = getattr(surface, "emit_tool_call_started", None)
    if callable(handler):
        handler(
            call_id,
            name,
            _event_preview(arguments),
            worker_id=worker_id,
            role=role,
        )


def _emit_tool_call_progress_event(
    surface: Surface | object,
    *,
    call_id: str,
    text: str,
    worker_id: str | None = None,
    role: str | None = None,
) -> None:
    handler = getattr(surface, "emit_tool_call_progress", None)
    if callable(handler):
        handler(call_id, text, worker_id=worker_id, role=role)


def _emit_tool_call_completed_event(
    surface: Surface | object,
    *,
    call_id: str,
    success: bool,
    result: Any,
    worker_id: str | None = None,
    role: str | None = None,
) -> None:
    handler = getattr(surface, "emit_tool_call_completed", None)
    if callable(handler):
        handler(
            call_id,
            success,
            _event_preview(result),
            worker_id=worker_id,
            role=role,
        )
