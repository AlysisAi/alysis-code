"""Stable machine-readable event protocol for Forge commands.

Human Rich output stays the default. When a Forge command runs with ``--machine`` it
suppresses Rich output and writes newline-delimited JSON to stdout instead: exactly one
JSON object per line, using this versioned envelope::

    {"v": 1, "event": "<name>", "ts": "<iso8601>", "run_id": "<id>|null", "data": {...}}

Consumers parse the envelope, not prose. Adding a field to ``data`` is a compatible
change; renaming an event or changing the envelope shape requires bumping ``v``.

INVARIANT: every Forge command invocation emits exactly one terminal event
(``run_completed`` or ``error``) before the process exits -- on success, on handled
failure, on an unhandled exception, on ``typer.Exit`` and on ``KeyboardInterrupt``.
:class:`ForgeMachineSession` is what guarantees it, so command bodies never have to
remember to emit one from their own error paths. Events emitted after the terminal one
are dropped rather than appended, so the "exactly one" half of the invariant holds even
if a late code path tries to say more.

Exit-code contract (see also :mod:`alysis_code.cli_impl.commands.forge`):

- ``0`` -- the command did its job. A review that rejects work exits 0.
- ``1`` -- the command ran to completion but the work was not accepted (task failed,
  verification blocked, swarm left tasks unfinished).
- ``2`` -- the command itself failed (bad config, missing run, unhandled exception).
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, TextIO

from .verification_repair import TASK_STATUS_COMPLETED_UNVERIFIED

SCHEMA_VERSION = 1

EVENT_RUN_STARTED = "run_started"
EVENT_PLAN_SAVED = "plan_saved"
EVENT_PLAN_INVALID = "plan_invalid"
EVENT_TASK_STARTED = "task_started"
EVENT_TASK_COMPLETED = "task_completed"
EVENT_TASK_FAILED = "task_failed"
EVENT_SCOPE_AMENDED = "scope_amended"
EVENT_VERIFICATION_RESULT = "verification_result"
EVENT_VERIFICATION_UNAVAILABLE = "verification_unavailable"
EVENT_REVIEW_RESULT = "review_result"
EVENT_RUN_COMPLETED = "run_completed"
EVENT_ERROR = "error"

EVENT_NAMES: frozenset[str] = frozenset(
    {
        EVENT_RUN_STARTED,
        EVENT_PLAN_SAVED,
        EVENT_PLAN_INVALID,
        EVENT_TASK_STARTED,
        EVENT_TASK_COMPLETED,
        EVENT_TASK_FAILED,
        EVENT_SCOPE_AMENDED,
        EVENT_VERIFICATION_RESULT,
        EVENT_VERIFICATION_UNAVAILABLE,
        EVENT_REVIEW_RESULT,
        EVENT_RUN_COMPLETED,
        EVENT_ERROR,
    }
)

TERMINAL_EVENT_NAMES: frozenset[str] = frozenset({EVENT_RUN_COMPLETED, EVENT_ERROR})

# Exit codes, kept here so producers and consumers read them from one place.
EXIT_OK = 0
EXIT_NOT_ACCEPTED = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130

# Task statuses the plan uses, mapped to the task lifecycle events.
# ``completed_unverified`` is a completion, not a failure: the work landed and was
# kept, and only the check for it was missing. Reporting it as ``task_failed`` is
# what made "no test runner in this repo" look like broken work.
_TASK_STARTED_STATUSES = frozenset({"in_progress"})
_TASK_COMPLETED_STATUSES = frozenset(
    {"done", "already_satisfied", TASK_STATUS_COMPLETED_UNVERIFIED}
)
_TASK_FAILED_STATUSES = frozenset(
    {"failed", "verify_failed", "changes_requested", "merge_conflict", "interrupted", "blocked"}
)


class ForgeEventProtocolError(ValueError):
    """Raised when an event name outside the declared schema is emitted."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def task_event_for_status(status: str) -> str | None:
    """Return the lifecycle event a plan task status implies, if any.

    Intermediate statuses (``planned``, ``todo``, ``ready_for_merge``) map to ``None``:
    they are bookkeeping, not something a consumer should render as a task transition.
    """
    normalized = str(status or "").strip().lower()
    if normalized in _TASK_STARTED_STATUSES:
        return EVENT_TASK_STARTED
    if normalized in _TASK_COMPLETED_STATUSES:
        return EVENT_TASK_COMPLETED
    if normalized in _TASK_FAILED_STATUSES:
        return EVENT_TASK_FAILED
    return None


class ForgeEventEmitter:
    """Writes the NDJSON event stream for one Forge command invocation.

    A disabled emitter is a no-op, so command bodies can call ``emit`` unconditionally
    and human mode stays byte-for-byte unchanged.
    """

    def __init__(
        self,
        *,
        command: str,
        enabled: bool = True,
        stream: TextIO | None = None,
        run_id: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.command = str(command)
        self.enabled = bool(enabled)
        self._stream = stream
        self._run_id = str(run_id).strip() or None if run_id else None
        self._clock = clock or _now_iso
        self._lock = threading.RLock()
        self._terminal_event: str | None = None
        self._emitted = 0

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def terminal_event(self) -> str | None:
        """Name of the terminal event already emitted, or ``None``."""
        return self._terminal_event

    @property
    def emitted_count(self) -> int:
        return self._emitted

    def set_run_id(self, run_id: str | None) -> None:
        """Attach a run id to every subsequent event once the run is known."""
        with self._lock:
            normalized = str(run_id).strip() if run_id is not None else ""
            self._run_id = normalized or None

    def emit(
        self,
        event: str,
        data: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
    ) -> bool:
        """Emit one event. Returns True when a line was written."""
        name = str(event or "").strip()
        if name not in EVENT_NAMES:
            raise ForgeEventProtocolError(
                f"Unknown forge event: {event!r}. Declared events: {sorted(EVENT_NAMES)}"
            )
        with self._lock:
            if not self.enabled:
                return False
            if self._terminal_event is not None:
                # The terminal event has been written; the stream is closed for business.
                return False
            if run_id is not None:
                self.set_run_id(run_id)
            payload = {
                "v": SCHEMA_VERSION,
                "event": name,
                "ts": self._clock(),
                "run_id": self._run_id,
                "data": dict(data or {}),
            }
            self._write(payload)
            self._emitted += 1
            if name in TERMINAL_EVENT_NAMES:
                self._terminal_event = name
            return True

    def run_completed(
        self,
        *,
        ok: bool,
        exit_code: int | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> bool:
        """Terminal event: the command ran to completion."""
        resolved_exit = (
            exit_code if exit_code is not None else (EXIT_OK if ok else EXIT_NOT_ACCEPTED)
        )
        payload: dict[str, Any] = {
            "command": self.command,
            "ok": bool(ok),
            "exit_code": int(resolved_exit),
        }
        payload.update(dict(data or {}))
        return self.emit(EVENT_RUN_COMPLETED, payload)

    def error(
        self,
        *,
        message: str,
        kind: str = "error",
        exit_code: int = EXIT_ERROR,
        data: Mapping[str, Any] | None = None,
    ) -> bool:
        """Terminal event: the command itself failed."""
        payload: dict[str, Any] = {
            "command": self.command,
            "kind": str(kind),
            "message": str(message),
            "exit_code": int(exit_code),
        }
        payload.update(dict(data or {}))
        return self.emit(EVENT_ERROR, payload)

    def _write(self, payload: Mapping[str, Any]) -> None:
        stream = self._stream if self._stream is not None else sys.stdout
        # ensure_ascii keeps the line encodable on every console codepage; default=str
        # keeps a stray Path or datetime from turning one event into a crash.
        line = json.dumps(payload, ensure_ascii=True, default=str, separators=(",", ":"))
        try:
            stream.write(line + "\n")
            stream.flush()
        except (OSError, ValueError):
            # A closed or broken stdout must not take the command down with it.
            return


_ACTIVE_LOCK = threading.RLock()
_ACTIVE_EMITTER: ForgeEventEmitter | None = None


def active_emitter() -> ForgeEventEmitter | None:
    """Return the emitter for the Forge command currently running, if any.

    Deep execution code (the swarm orchestrator) uses this instead of threading an
    emitter through dozens of call sites. One process runs one Forge command, so a
    process-global registration is the honest model here.
    """
    with _ACTIVE_LOCK:
        return _ACTIVE_EMITTER


def set_active_emitter(emitter: ForgeEventEmitter | None) -> ForgeEventEmitter | None:
    """Register the active emitter and return the one it replaced."""
    global _ACTIVE_EMITTER
    with _ACTIVE_LOCK:
        previous = _ACTIVE_EMITTER
        _ACTIVE_EMITTER = emitter
        return previous


def machine_output_active() -> bool:
    """True when stdout belongs to the NDJSON stream and Rich output must stay silent."""
    emitter = active_emitter()
    return emitter is not None and emitter.enabled


def emit_active(
    event: str,
    data: Mapping[str, Any] | None = None,
    *,
    run_id: str | None = None,
) -> bool:
    """Emit through the active emitter; a no-op when no Forge command is machine-mode."""
    emitter = active_emitter()
    if emitter is None:
        return False
    return emitter.emit(event, data, run_id=run_id)


def emit_task_status(
    task_id: str,
    status: str,
    data: Mapping[str, Any] | None = None,
) -> bool:
    """Emit the task lifecycle event a plan status transition implies."""
    event = task_event_for_status(status)
    if event is None:
        return False
    payload: dict[str, Any] = {"task_id": str(task_id), "status": str(status)}
    payload.update(dict(data or {}))
    return emit_active(event, payload)


def _exit_code_from_exception(exc: BaseException) -> int | None:
    """Extract the exit code from typer.Exit / click.exceptions.Exit / SystemExit."""
    for attribute in ("exit_code", "code"):
        if not hasattr(exc, attribute):
            continue
        raw = getattr(exc, attribute)
        if raw is None:
            return 0
        if isinstance(raw, bool):
            return int(raw)
        if isinstance(raw, int):
            return raw
    return None


class ForgeMachineSession:
    """Context manager that enforces the one-terminal-event invariant.

    Wrap a command body in this and every exit path -- return, ``typer.Exit``,
    ``KeyboardInterrupt``, unhandled exception -- ends with exactly one terminal event.
    Exceptions are never swallowed: the event is written, then the exception propagates
    so exit codes and tracebacks behave exactly as they do in human mode.
    """

    def __init__(self, emitter: ForgeEventEmitter, *, register_active: bool = True) -> None:
        self.emitter = emitter
        self._register_active = bool(register_active)
        self._previous: ForgeEventEmitter | None = None
        self._registered = False

    def __enter__(self) -> ForgeEventEmitter:
        if self._register_active:
            self._previous = set_active_emitter(self.emitter)
            self._registered = True
        return self.emitter

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any
    ) -> bool:
        try:
            self._finalize(exc)
        finally:
            if self._registered:
                set_active_emitter(self._previous)
                self._registered = False
        return False

    def _finalize(self, exc: BaseException | None) -> None:
        if not self.emitter.enabled or self.emitter.terminal_event is not None:
            return

        if exc is None:
            self.emitter.run_completed(ok=True, exit_code=EXIT_OK)
            return

        if isinstance(exc, KeyboardInterrupt):
            self.emitter.error(
                message="Interrupted.",
                kind="interrupted",
                exit_code=EXIT_INTERRUPTED,
            )
            return

        exit_code = _exit_code_from_exception(exc)
        if exit_code is not None:
            # A command that exits without describing itself still gets a truthful
            # terminal event, mapped through the exit-code contract in this module.
            if exit_code == EXIT_OK:
                self.emitter.run_completed(ok=True, exit_code=EXIT_OK)
            elif exit_code == EXIT_NOT_ACCEPTED:
                self.emitter.run_completed(ok=False, exit_code=EXIT_NOT_ACCEPTED)
            else:
                self.emitter.error(
                    message=str(exc) or f"Command exited with code {exit_code}.",
                    kind="exit",
                    exit_code=exit_code,
                )
            return

        self.emitter.error(
            message=str(exc) or exc.__class__.__name__,
            kind="exception",
            exit_code=EXIT_ERROR,
            data={"exception_type": exc.__class__.__name__},
        )


@contextmanager
def machine_session(
    command: str,
    *,
    machine: bool,
    stream: TextIO | None = None,
    run_id: str | None = None,
    clock: Callable[[], str] | None = None,
) -> Iterator[ForgeEventEmitter]:
    """Open an event session for one Forge command invocation.

    When ``machine`` is false the yielded emitter is inert and nothing is written, so
    the same command body serves both the human TUI and the machine protocol.
    """
    emitter = ForgeEventEmitter(
        command=command,
        enabled=machine,
        stream=stream,
        run_id=run_id,
        clock=clock,
    )
    with ForgeMachineSession(emitter, register_active=machine) as active:
        yield active


def parse_event_line(line: str) -> dict[str, Any] | None:
    """Parse one output line as a Forge event, or return None if it is not one.

    Tolerant by design: job logs interleave worker output with the event stream, so a
    line that is not a well-formed event envelope is simply not an event.
    """
    text = str(line or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != SCHEMA_VERSION:
        return None
    event = payload.get("event")
    if not isinstance(event, str) or event not in EVENT_NAMES:
        return None
    if not isinstance(payload.get("data"), dict):
        return None
    return payload


def is_terminal_event(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("event") or "") in TERMINAL_EVENT_NAMES


__all__ = [
    "EVENT_ERROR",
    "EVENT_NAMES",
    "EVENT_PLAN_INVALID",
    "EVENT_PLAN_SAVED",
    "EVENT_REVIEW_RESULT",
    "EVENT_RUN_COMPLETED",
    "EVENT_RUN_STARTED",
    "EVENT_SCOPE_AMENDED",
    "EVENT_TASK_COMPLETED",
    "EVENT_TASK_FAILED",
    "EVENT_TASK_STARTED",
    "EVENT_VERIFICATION_RESULT",
    "EVENT_VERIFICATION_UNAVAILABLE",
    "EXIT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_NOT_ACCEPTED",
    "EXIT_OK",
    "SCHEMA_VERSION",
    "TERMINAL_EVENT_NAMES",
    "ForgeEventEmitter",
    "ForgeEventProtocolError",
    "ForgeMachineSession",
    "active_emitter",
    "emit_active",
    "emit_task_status",
    "is_terminal_event",
    "machine_output_active",
    "machine_session",
    "parse_event_line",
    "set_active_emitter",
    "task_event_for_status",
]
