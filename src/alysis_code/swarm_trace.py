from __future__ import annotations

import json
import queue
import threading
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from rich.console import Console

from .session_store import SessionStore
from .subagent_labels import subagent_identity
from .surface.base import Surface
from .surface.rich_surface import (
    _format_duration_ms,
    _redact,
    _summarize_tool_output,
    _tool_display_name,
    _tool_input_preview,
    _truncate_inline,
)
from .surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)

_TRACE_LEVELS = {"off", "compact", "full"}
_SENTINEL = object()


def normalize_swarm_trace_level(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _TRACE_LEVELS:
        return normalized
    return "off"


def _now_ts() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_trace_text(text: str, *, max_chars: int = 320) -> str:
    clean = _redact(str(text or "").strip())
    clean = " ".join(clean.split())
    if len(clean) <= max_chars:
        return clean
    if max_chars <= 3:
        return clean[:max_chars]
    return clean[: max_chars - 3] + "..."


@dataclass(frozen=True, slots=True)
class SwarmTraceEvent:
    ts: str
    run_id: str
    phase: str
    message: str
    verbosity: str = "compact"
    task_id: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "phase": self.phase,
            "verbosity": self.verbosity,
            "message": self.message,
        }


def build_swarm_trace_event(
    *,
    run_id: str,
    phase: str,
    message: str,
    verbosity: str = "compact",
    task_id: str | None = None,
) -> SwarmTraceEvent:
    normalized_verbosity = "full" if verbosity == "full" else "compact"
    return SwarmTraceEvent(
        ts=_now_ts(),
        run_id=run_id,
        phase=_sanitize_trace_text(phase, max_chars=80) or "swarm",
        message=_sanitize_trace_text(message),
        verbosity=normalized_verbosity,
        task_id=str(task_id or "").strip() or None,
    )


def format_swarm_trace_message(event: SwarmTraceEvent) -> str:
    prefix = f"[{event.task_id}] " if event.task_id else ""
    return f"{prefix}{event.message}"


class SwarmTraceSink(Protocol):
    def emit(self, event: SwarmTraceEvent) -> None: ...

    def close(self) -> None: ...


class NoopSwarmTraceSink:
    def emit(self, event: SwarmTraceEvent) -> None:
        _ = event

    def close(self) -> None:
        return None


class CompositeSwarmTraceSink:
    def __init__(self, *sinks: SwarmTraceSink) -> None:
        self._sinks = [sink for sink in sinks if sink is not None]

    def emit(self, event: SwarmTraceEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                continue

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                continue


class SerializedSwarmTraceSink:
    def __init__(
        self,
        *,
        artifact_path: Path,
        trace_level: str,
        surface: Surface | None = None,
        session_store: SessionStore | None = None,
        console: Console | None = None,
        store_source: str = "swarm_trace",
    ) -> None:
        self.artifact_path = artifact_path
        self.trace_level = normalize_swarm_trace_level(trace_level)
        self.surface = surface
        self.session_store = session_store
        self.console = console
        self.store_source = store_source
        self._queue: queue.Queue[SwarmTraceEvent | object] = queue.Queue()
        self._closed = False
        self._close_lock = threading.Lock()
        self.write_failures = 0
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.artifact_path.open("a", encoding="utf-8")
        self._thread = threading.Thread(target=self._consume, daemon=True)
        self._thread.start()

    def emit(self, event: SwarmTraceEvent) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._queue.put(event)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(_SENTINEL)
        self._thread.join()
        try:
            self._fh.flush()
        finally:
            self._fh.close()

    def _consume(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                break
            event = item
            if not isinstance(event, SwarmTraceEvent):
                continue
            self._write_artifact(event)
            self._render_live(event)

    def _write_artifact(self, event: SwarmTraceEvent) -> None:
        try:
            self._fh.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as exc:  # noqa: BLE001 - a trace write must not kill the run
            # Non-silent: a broken swarm-trace artifact must be observable, not a
            # silently-truncated trace an autonomous harness would misread as complete.
            self.write_failures += 1
            if self.write_failures == 1:
                warnings.warn(
                    f"swarm trace write to {self.artifact_path} failed "
                    f"({type(exc).__name__}: {exc}); subsequent failures are counted but "
                    "not re-warned.",
                    RuntimeWarning,
                    stacklevel=2,
                )

    def _render_live(self, event: SwarmTraceEvent) -> None:
        if self.trace_level == "off":
            return
        if event.verbosity == "full" and self.trace_level != "full":
            return
        message = format_swarm_trace_message(event)
        append = getattr(self.session_store, "append", None)
        if callable(append):
            try:
                append(
                    "progress",
                    {
                        "message": message,
                        "source": self.store_source,
                        "phase": event.phase,
                        "task_id": event.task_id,
                        "verbosity": event.verbosity,
                    },
                )
            except Exception:
                pass
        # A surface that wants structured per-task progress (the TUI's live forge
        # view) opts in with ``on_swarm_event``; it is preferred over the flat
        # ``on_progress_update`` string hook. RichSurface defines neither beyond
        # ``on_progress_update`` so the classic path is unchanged.
        swarm_handler = getattr(self.surface, "on_swarm_event", None)
        if callable(swarm_handler):
            try:
                swarm_handler(event)
                return
            except Exception:
                pass
        handler = getattr(self.surface, "on_progress_update", None)
        if callable(handler):
            try:
                handler(message)
                return
            except Exception:
                pass
        if self.console is not None:
            try:
                self.console.print(f"[dim]{message}[/dim]")
            except Exception:
                return


def emit_swarm_trace(
    sink: SwarmTraceSink | None,
    *,
    run_id: str,
    phase: str,
    message: str,
    task_id: str | None = None,
    verbosity: str = "compact",
) -> None:
    if sink is None:
        return
    sink.emit(
        build_swarm_trace_event(
            run_id=run_id,
            phase=phase,
            message=message,
            task_id=task_id,
            verbosity=verbosity,
        )
    )


class SwarmWorkerTraceSurface:
    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        trace_sink: SwarmTraceSink,
        trace_level: str,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.trace_sink = trace_sink
        self.trace_level = normalize_swarm_trace_level(trace_level)
        self._tool_output_summary: dict[str, str] = {}
        self._assistant_started = False
        self._assistant_chars = 0
        self._assistant_full_bucket = -1
        self._last_progress = ""

    def _emit(self, phase: str, message: str, *, verbosity: str = "compact") -> None:
        if self.trace_level == "off":
            return
        if verbosity == "full" and self.trace_level != "full":
            return
        emit_swarm_trace(
            self.trace_sink,
            run_id=self.run_id,
            task_id=self.task_id,
            phase=phase,
            message=message,
            verbosity=verbosity,
        )

    def on_status_update(self, status: StatusEvent) -> None:
        _ = status

    def on_user_message(self, text: str) -> None:
        _ = text

    def on_assistant_token(self, delta: str) -> None:
        if self.trace_level == "off" or not delta:
            return
        self._assistant_chars += len(delta)
        if not self._assistant_started:
            self._assistant_started = True
            self._emit("worker.output", "Receiving worker output...")
        if self.trace_level != "full":
            return
        bucket = self._assistant_chars // 320
        if bucket <= self._assistant_full_bucket:
            return
        self._assistant_full_bucket = bucket
        self._emit(
            "worker.output",
            f"Worker output progress: ~{self._assistant_chars} chars captured.",
            verbosity="full",
        )

    def on_assistant_message_done(self, text: str) -> None:
        clean = str(text or "").strip()
        if self.trace_level == "off" or not clean:
            return
        message = f"Worker response ready ({len(clean)} chars)."
        self._emit("worker.output", message)
        if self.trace_level == "full":
            preview = _truncate_inline(clean, max_chars=140)
            if preview:
                self._emit(
                    "worker.output",
                    f"Worker response preview: {preview}",
                    verbosity="full",
                )

    def on_progress_update(self, message: str) -> None:
        clean = _sanitize_trace_text(message, max_chars=220)
        if not clean or clean == self._last_progress:
            return
        self._last_progress = clean
        self._emit("worker.progress", clean)

    def on_subagent_start(self, event: SubagentStartEvent) -> None:
        display_name = subagent_identity(event.name, event.label)
        self._emit(
            "worker.subagent",
            f'Subagent "{display_name}" started (mode={event.mode}).',
        )

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        display_name = subagent_identity(event.name, event.label)
        status_label = "finished" if event.status == "success" else event.status
        message = (
            f'Subagent "{display_name}" {status_label} '
            f"({event.steps_completed} step(s), {_format_duration_ms(event.elapsed_ms)})."
        )
        if event.error:
            message += f" {_truncate_inline(str(event.error), max_chars=140)}"
        self._emit("worker.subagent", message)

    def on_tool_start(self, event: ToolStartEvent) -> None:
        display = _tool_display_name(event.name)
        self._emit("worker.tool", f"Step {event.step}: {display}")
        if self.trace_level != "full":
            return
        self._emit(
            "worker.tool",
            f"Input: {_tool_input_preview(event.name, event.args)}",
            verbosity="full",
        )

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        summary = _summarize_tool_output(event.name, event.chunk).strip()
        if summary:
            self._tool_output_summary[event.tool_call_id] = summary

    def on_tool_end(self, event: ToolEndEvent) -> None:
        display = _tool_display_name(event.name)
        elapsed = _format_duration_ms(event.elapsed_ms)
        detail = ""
        err = event.meta.get("error")
        if err:
            detail = f": {_truncate_inline(str(err), max_chars=140)}"
        summary = self._tool_output_summary.pop(event.tool_call_id, "").strip()
        if event.status == "done":
            message = f"{display}: {summary} ({elapsed})" if summary else f"{display} ({elapsed})"
            self._emit("worker.tool", message)
            return
        self._emit("worker.tool", f"{display} failed ({elapsed}){detail}")

    def on_patch_generated(self, event: PatchEvent) -> None:
        if self.trace_level != "full":
            return
        summary = event.summary.strip() or ", ".join(event.files[:3]) or "patch prepared"
        self._emit(
            "worker.patch",
            f"Patch generated for {len(event.files)} file(s): {_truncate_inline(summary, max_chars=140)}",
            verbosity="full",
        )

    def on_warning(self, warning: str) -> None:
        clean = _sanitize_trace_text(warning, max_chars=220)
        if clean:
            self._emit("worker.warning", f"Worker warning: {clean}")

    def on_error(self, err: str) -> None:
        clean = _sanitize_trace_text(err, max_chars=220)
        if clean:
            self._emit("worker.error", f"Worker error: {clean}")

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        _ = request
        return ApprovalDecision(allow=False)
