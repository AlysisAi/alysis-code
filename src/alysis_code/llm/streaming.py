from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .types import LLMError


@dataclass(frozen=True)
class SSEFrame:
    """A parsed Server-Sent Events frame.

    The parser intentionally keeps this structure provider-neutral so native
    Responses, Messages, and GenerateContent streaming paths can share it.
    """

    event: str
    data: str
    event_id: str | None = None
    retry: int | None = None


def iter_sse_frames(
    lines: Iterable[str | bytes],
    *,
    done_sentinels: tuple[str, ...] = ("[DONE]",),
    include_done: bool = False,
) -> Iterator[SSEFrame]:
    """Yield SSE frames from an iterable of response lines.

    Handles `event:`, multi-line `data:`, comments, blank-line frame
    boundaries, optional `id:` / `retry:`, and `[DONE]`-style sentinels.
    """

    event = "message"
    event_id: str | None = None
    retry: int | None = None
    data_lines: list[str] = []

    def _flush() -> SSEFrame | None:
        nonlocal event, event_id, retry, data_lines
        if not data_lines and event == "message" and event_id is None and retry is None:
            return None
        frame = SSEFrame(
            event=event or "message",
            data="\n".join(data_lines),
            event_id=event_id,
            retry=retry,
        )
        event = "message"
        event_id = None
        retry = None
        data_lines = []
        return frame

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = raw_line
        line = line.rstrip("\r\n")
        if line == "":
            frame = _flush()
            if frame is None:
                continue
            if frame.data.strip() in done_sentinels and not include_done:
                return
            yield frame
            continue
        if line.startswith(":"):
            continue

        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""

        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None

    frame = _flush()
    if frame is not None:
        if frame.data.strip() in done_sentinels and not include_done:
            return
        yield frame


def parse_sse_json_frame(frame: SSEFrame, *, stream_name: str) -> Any:
    """Parse a frame's JSON payload with a deterministic provider error."""

    try:
        return json.loads(frame.data)
    except json.JSONDecodeError as exc:
        event_suffix = f" event={frame.event!r}" if frame.event else ""
        raise LLMError(f"{stream_name} emitted malformed JSON{event_suffix}: {exc.msg}") from exc
