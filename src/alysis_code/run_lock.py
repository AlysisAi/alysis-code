"""The lock that keeps two Forge executions off one workspace.

A lock file is only useful if it can also be *un*-stuck. The owner therefore
publishes two independent liveness signals, and a would-be acquirer needs only
one of them to conclude the owner is gone:

* **pid** -- the owner's process id on the owner's host. `kill -9` clears this
  instantly, but it lies when the id has been recycled by an unrelated process.
* **heartbeat** -- a timestamp the owner refreshes every
  ``heartbeat_interval_s`` and promises to keep fresher than ``heartbeat_ttl_s``.
  A recycled pid cannot forge it, and a hung owner stops producing it.

Only a lock that *declared* the heartbeat contract can be reaped by it: a lock
written by an older build never promised to beat, so its silence proves nothing.
A heartbeat-expired lock is also re-read after one grace interval before it is
recovered, so a machine that merely came back from sleep gets to prove it is
alive instead of having its lock taken.

Everything else still fails closed. A lock owned by another host is ambiguous by
construction -- this process cannot probe that host's process table -- so it
blocks, and the error says exactly how old the lock is, who holds it, and which
command clears it.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json
from .branding import env_get
from .forge import ForgeError, now_iso

# 2 added the heartbeat contract (``heartbeat_interval_s``/``heartbeat_ttl_s``).
# Readers must keep understanding version 1 locks; they simply never qualify for
# TTL-based recovery.
LOCK_SCHEMA_VERSION = 2
_LOCK_FILE_NAME = "active_execution.lock.json"
_RECOVERY_FILE_NAME = "active_execution.recovering.json"
_EVENTS_FILE_NAME = "active_execution.events.jsonl"
_WAITING_FILE_PREFIX = "active_execution.waiting."
_WAITING_FILE_SUFFIX = ".json"
_DEFAULT_POLL_INTERVAL_S = 1.0
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_ERROR_INVALID_PARAMETER = 87

DEFAULT_HEARTBEAT_INTERVAL_S = 15.0
# Eight missed beats. Long enough that a busy machine, a slow filesystem or a
# short suspend never trips it; short enough that a recycled pid unblocks the
# workspace in a couple of minutes rather than never.
DEFAULT_HEARTBEAT_TTL_S = 120.0
_MIN_HEARTBEAT_INTERVAL_S = 0.05
_MAX_RECOVERY_GRACE_S = 5.0

_HEARTBEAT_ENABLED_ENV = "ALYSIS_RUN_LOCK_HEARTBEAT"
_HEARTBEAT_INTERVAL_ENV = "ALYSIS_RUN_LOCK_HEARTBEAT_INTERVAL_S"
_HEARTBEAT_TTL_ENV = "ALYSIS_RUN_LOCK_TTL_S"

STALENESS_ACTIVE = "active"
STALENESS_STALE = "stale"
STALENESS_AMBIGUOUS = "ambiguous"

STALE_KIND_DEAD_PID = "dead_pid"
STALE_KIND_HEARTBEAT_EXPIRED = "heartbeat_expired"

UNLOCK_COMMAND = "alysis forge unlock"


class RunMutationConflictError(ForgeError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "active_workspace_execution",
        metadata: dict[str, Any] | None = None,
        diagnostic: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.metadata = metadata
        self.diagnostic = diagnostic or message


class _HeartbeatWorker:
    """Refreshes one lock's heartbeat on a daemon thread until it is stopped.

    A lock whose owner stops beating gets reaped by its own TTL, so the one thing
    this must never do is give up. A single refresh can fail for reasons that have
    nothing to do with the owner's health -- on Windows an ``os.replace`` races a
    concurrent reader of the same lock file and raises ``PermissionError`` -- and
    treating that as fatal would silently hand the workspace to the next acquirer
    while this process is still mutating it. So every failure is counted and
    retried on the next tick, and only :meth:`stop` ends the loop.

    Failures are never raised either: a heartbeat that cannot be written must not
    take down the execution it exists to protect.
    """

    def __init__(self, *, refresh: Callable[[], None], interval_s: float) -> None:
        self._refresh = refresh
        self._interval_s = max(_MIN_HEARTBEAT_INTERVAL_S, float(interval_s))
        self._stop = threading.Event()
        self._consecutive_failures = 0
        self._thread = threading.Thread(
            target=self._loop,
            name="alysis-run-lock-heartbeat",
            daemon=True,
        )

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._refresh()
            except Exception:  # noqa: BLE001 - see class docstring
                self._consecutive_failures += 1
                continue
            self._consecutive_failures = 0


@dataclass(frozen=True)
class RunMutationGuard:
    run_id: str
    mode: str
    run_dir: Path
    workspace_root: Path
    owner_token: str
    lock_path: Path
    recovery_path: Path
    acquired_after_wait: bool = False
    wait_started_at: str | None = None
    wait_finished_at: str | None = None
    wait_record_path: Path | None = None
    heartbeat: _HeartbeatWorker | None = None

    def __enter__(self) -> RunMutationGuard:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type
        _ = exc
        _ = tb
        self.release()

    def __del__(self) -> None:
        with suppress(Exception):
            self.release()

    def release(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.stop()
        current = _load_metadata(self.lock_path)
        if current is None:
            return
        if str(current.get("owner_token") or "").strip() != self.owner_token:
            return
        with suppress(FileNotFoundError):
            self.lock_path.unlink()
        if self.wait_record_path is not None:
            with suppress(FileNotFoundError):
                self.wait_record_path.unlink()
        current_recovery = _load_metadata(self.recovery_path)
        if current_recovery is None:
            return
        if str(current_recovery.get("owner_token") or "").strip() != self.owner_token:
            return
        with suppress(FileNotFoundError):
            self.recovery_path.unlink()

    def refresh_heartbeat(self) -> None:
        current = _load_metadata(self.lock_path)
        if current is None:
            return
        if str(current.get("owner_token") or "").strip() != self.owner_token:
            return
        current["last_heartbeat_at"] = now_iso()
        atomic_write_json(self.lock_path, current)


def acquire_run_mutation_guard(
    *,
    run_id: str,
    mode: str,
    run_dir: Path,
    workspace_root: Path,
    wait: bool = False,
    wait_timeout_s: float | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    on_wait: Callable[[dict[str, Any]], None] | None = None,
    owner_session_id: str | None = None,
) -> RunMutationGuard:
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / _LOCK_FILE_NAME
    recovery_path = run_dir / _RECOVERY_FILE_NAME
    events_path = run_dir / _EVENTS_FILE_NAME
    owner_token = uuid.uuid4().hex
    owner_id = _owner_id(owner_token=owner_token, owner_session_id=owner_session_id)
    wait_started_monotonic = time.monotonic()
    wait_started_at: str | None = None
    wait_finished_at: str | None = None
    wait_record_path: Path | None = None
    acquired_after_wait = False
    wait_notice_emitted = False

    while True:
        _clear_stale_recovery_claim(
            recovery_path=recovery_path,
            run_id=run_id,
            mode=mode,
        )
        metadata = _build_metadata(
            run_id=run_id,
            mode=mode,
            workspace_root=workspace_root,
            run_dir=run_dir,
            owner_token=owner_token,
            owner_id=owner_id,
            owner_session_id=owner_session_id,
            kind="lock",
        )
        metadata_text = _metadata_text(metadata)
        try:
            _write_exclusive(lock_path, metadata_text)
        except FileExistsError:
            existing_metadata, existing_text = _load_metadata_with_text(lock_path)
            if existing_metadata is None or existing_text is None:
                raise _conflict_error(
                    run_id=run_id,
                    mode=mode,
                    metadata=None,
                    note="the active run lock exists but its metadata is unreadable, so recovery is blocked",
                ) from None
            staleness = assess_lock_staleness(existing_metadata)
            stale_reason = staleness.reason if staleness.recoverable else None
            if stale_reason is None:
                conflict = _conflict_error(
                    run_id=run_id,
                    mode=mode,
                    metadata=existing_metadata,
                )
                if not wait:
                    raise conflict from None
                now_monotonic = time.monotonic()
                if wait_timeout_s is not None and now_monotonic - wait_started_monotonic >= max(
                    0.0, wait_timeout_s
                ):
                    if wait_record_path is not None:
                        _append_event(
                            events_path,
                            {
                                "schema_version": LOCK_SCHEMA_VERSION,
                                "event": "queued_wait_timed_out",
                                "reason_code": "active_execution_wait_timeout",
                                "run_id": run_id,
                                "mode": mode,
                                "owner_id": owner_id,
                                "workspace_root": _safe_resolved_path(workspace_root),
                                "workspace_identity": normalize_workspace_identity_path(
                                    workspace_root
                                ),
                                "run_dir": _safe_resolved_path(run_dir),
                                "wait_started_at": wait_started_at,
                            },
                        )
                        with suppress(FileNotFoundError):
                            wait_record_path.unlink()
                    raise _conflict_error(
                        run_id=run_id,
                        mode=mode,
                        metadata=existing_metadata,
                        note=(
                            "queued execution timed out waiting for the active mutation guard "
                            "to finish"
                        ),
                        reason_code="active_execution_wait_timeout",
                    ) from None
                if wait_started_at is None:
                    wait_started_at = now_iso()
                    acquired_after_wait = True
                    wait_record_path = run_dir / (
                        f"{_WAITING_FILE_PREFIX}{owner_token}{_WAITING_FILE_SUFFIX}"
                    )
                    wait_payload = _build_wait_metadata(
                        run_id=run_id,
                        mode=mode,
                        workspace_root=workspace_root,
                        run_dir=run_dir,
                        owner_token=owner_token,
                        owner_id=owner_id,
                        owner_session_id=owner_session_id,
                        blocked_by=existing_metadata,
                        started_at=wait_started_at,
                        diagnostic=conflict.diagnostic,
                    )
                    atomic_write_json(wait_record_path, wait_payload)
                    _append_event(events_path, {**wait_payload, "event": "queued_wait_started"})
                if on_wait is not None and not wait_notice_emitted:
                    on_wait(
                        {
                            "reason_code": conflict.reason_code,
                            "diagnostic": conflict.diagnostic,
                            "blocked_by": _public_lock_metadata(existing_metadata),
                            "run_id": run_id,
                            "mode": mode,
                            "wait_started_at": wait_started_at,
                        }
                    )
                    wait_notice_emitted = True
                time.sleep(max(0.05, min(float(poll_interval_s), 5.0)))
                continue
            recovery_metadata = _build_metadata(
                run_id=run_id,
                mode=mode,
                workspace_root=workspace_root,
                run_dir=run_dir,
                owner_token=owner_token,
                owner_id=owner_id,
                owner_session_id=owner_session_id,
                kind="recovery",
                recovery_reason=stale_reason,
            )
            try:
                _write_exclusive(recovery_path, _metadata_text(recovery_metadata))
            except FileExistsError:
                _clear_stale_recovery_claim(
                    recovery_path=recovery_path,
                    run_id=run_id,
                    mode=mode,
                )
                raise _conflict_error(
                    run_id=run_id,
                    mode=mode,
                    metadata=existing_metadata,
                    note="another execution is already recovering the stale run lock",
                ) from None
            try:
                # A lock that only *looks* expired gets one grace interval to prove
                # otherwise. A machine coming back from suspend refreshes its
                # heartbeat within one interval, and the unchanged-text check below
                # then reads it as active. A dead pid needs no such courtesy.
                if staleness.kind == STALE_KIND_HEARTBEAT_EXPIRED:
                    time.sleep(_recovery_grace_s(existing_metadata))
                current_metadata, current_text = _load_metadata_with_text(lock_path)
                if current_metadata is None or current_text is None:
                    raise _conflict_error(
                        run_id=run_id,
                        mode=mode,
                        metadata=None,
                        note="the active run lock disappeared or became unreadable during recovery",
                    )
                if current_text != existing_text:
                    raise _conflict_error(
                        run_id=run_id,
                        mode=mode,
                        metadata=current_metadata,
                        note="the active run lock changed while recovery was in progress",
                    )
                if not assess_lock_staleness(current_metadata).recoverable:
                    raise _conflict_error(
                        run_id=run_id,
                        mode=mode,
                        metadata=current_metadata,
                        note="the active run lock is no longer definitely stale",
                    )
                lock_path.unlink()
                _append_event(
                    events_path,
                    {
                        **recovery_metadata,
                        "event": "stale_lock_recovered",
                        "recovered_owner": _public_lock_metadata(current_metadata),
                    },
                )
                try:
                    metadata = _build_metadata(
                        run_id=run_id,
                        mode=mode,
                        workspace_root=workspace_root,
                        run_dir=run_dir,
                        owner_token=owner_token,
                        owner_id=owner_id,
                        owner_session_id=owner_session_id,
                        kind="lock",
                    )
                    metadata_text = _metadata_text(metadata)
                    _write_exclusive(lock_path, metadata_text)
                except FileExistsError:
                    replacement_metadata = _load_metadata(lock_path)
                    raise _conflict_error(
                        run_id=run_id,
                        mode=mode,
                        metadata=replacement_metadata,
                        note="another execution claimed the run while stale-lock recovery was finalizing",
                    ) from None
            finally:
                current_recovery = _load_metadata(recovery_path)
                if (
                    current_recovery is not None
                    and str(current_recovery.get("owner_token") or "").strip() == owner_token
                ):
                    with suppress(FileNotFoundError):
                        recovery_path.unlink()
        break
    if acquired_after_wait:
        wait_finished_at = now_iso()
        _append_event(
            events_path,
            {
                "schema_version": LOCK_SCHEMA_VERSION,
                "event": "queued_wait_finished",
                "reason_code": "queued_execution_acquired_lock",
                "run_id": run_id,
                "mode": mode,
                "owner_id": owner_id,
                "workspace_root": _safe_resolved_path(workspace_root),
                "workspace_identity": normalize_workspace_identity_path(workspace_root),
                "run_dir": _safe_resolved_path(run_dir),
                "wait_started_at": wait_started_at,
                "wait_finished_at": wait_finished_at,
            },
        )
    guard = RunMutationGuard(
        run_id=run_id,
        mode=mode,
        run_dir=run_dir,
        workspace_root=workspace_root,
        owner_token=owner_token,
        lock_path=lock_path,
        recovery_path=recovery_path,
        acquired_after_wait=acquired_after_wait,
        wait_started_at=wait_started_at,
        wait_finished_at=wait_finished_at,
        wait_record_path=wait_record_path,
    )
    worker = _start_heartbeat_worker(guard)
    if worker is not None:
        # The guard is frozen so callers cannot repoint it at another lock; the
        # heartbeat is owner state that only exists once the guard does.
        object.__setattr__(guard, "heartbeat", worker)
    return guard


def _start_heartbeat_worker(guard: RunMutationGuard) -> _HeartbeatWorker | None:
    if not heartbeat_enabled():
        return None
    worker = _HeartbeatWorker(
        refresh=guard.refresh_heartbeat,
        interval_s=heartbeat_interval_s(),
    )
    worker.start()
    return worker


def inspect_run_mutation_lock(run_dir: Path) -> dict[str, Any] | None:
    return _load_metadata(run_dir / _LOCK_FILE_NAME)


def run_mutation_lock_path(run_dir: Path) -> Path:
    return run_dir / _LOCK_FILE_NAME


def heartbeat_enabled() -> bool:
    raw = env_get(_HEARTBEAT_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def heartbeat_interval_s() -> float:
    return _env_positive_float(_HEARTBEAT_INTERVAL_ENV, DEFAULT_HEARTBEAT_INTERVAL_S)


def heartbeat_ttl_s() -> float:
    ttl = _env_positive_float(_HEARTBEAT_TTL_ENV, DEFAULT_HEARTBEAT_TTL_S)
    # A TTL at or below the beat interval would reap healthy owners between beats.
    return max(ttl, heartbeat_interval_s() * 2.0)


def _env_positive_float(name: str, default: float) -> float:
    raw = env_get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class LockStaleness:
    """Why one lock file is (or is not) safe to take over.

    ``verdict`` is the only thing callers should branch on:

    * ``active`` -- the owner is provably alive. Never recover.
    * ``ambiguous`` -- liveness cannot be established from here (another host, an
      unreadable pid, a lock that never promised a heartbeat). Never recover.
    * ``stale`` -- the owner is provably gone. Safe to recover.
    """

    verdict: str
    reason: str
    kind: str | None = None
    same_host: bool = False
    pid: int | None = None
    hostname: str | None = None
    owner_id: str | None = None
    mode: str | None = None
    age_s: float | None = None
    heartbeat_age_s: float | None = None
    heartbeat_ttl_s: float | None = None
    process_running: bool | None = None

    @property
    def recoverable(self) -> bool:
        return self.verdict == STALENESS_STALE

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "kind": self.kind,
            "same_host": self.same_host,
            "pid": self.pid,
            "hostname": self.hostname,
            "owner_id": self.owner_id,
            "mode": self.mode,
            "age_s": self.age_s,
            "heartbeat_age_s": self.heartbeat_age_s,
            "heartbeat_ttl_s": self.heartbeat_ttl_s,
            "process_running": self.process_running,
        }


def assess_lock_staleness(
    metadata: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> LockStaleness:
    """Classify a lock file's owner as active, ambiguous, or provably gone."""
    if metadata is None:
        return LockStaleness(
            verdict=STALENESS_AMBIGUOUS,
            reason="lock metadata is missing or unreadable",
        )
    reference = now or datetime.now(UTC)
    hostname = _clean_str(metadata.get("hostname"))
    owner_id = _clean_str(metadata.get("owner_id"))
    mode = _clean_str(metadata.get("mode"))
    pid = _coerce_pid(metadata.get("pid"))
    age_s = _age_seconds(metadata.get("acquired_at"), reference)
    heartbeat_age_s = _age_seconds(metadata.get("last_heartbeat_at"), reference)
    declared_ttl = _coerce_positive_float(metadata.get("heartbeat_ttl_s"))
    same_host = hostname == socket.gethostname()

    base = {
        "kind": None,
        "same_host": same_host,
        "pid": pid,
        "hostname": hostname,
        "owner_id": owner_id,
        "mode": mode,
        "age_s": age_s,
        "heartbeat_age_s": heartbeat_age_s,
        "heartbeat_ttl_s": declared_ttl,
    }

    if not same_host:
        return LockStaleness(
            verdict=STALENESS_AMBIGUOUS,
            reason=(
                f"lock is held by another host ({hostname or 'unknown'}); this machine "
                "cannot tell whether that process is still running"
            ),
            **base,
        )

    process_running = _process_is_running(pid) if pid is not None else None
    base["process_running"] = process_running

    if process_running is False:
        return LockStaleness(
            verdict=STALENESS_STALE,
            reason="owner process is no longer running",
            **{**base, "kind": STALE_KIND_DEAD_PID},
        )

    # A pid that still resolves is not proof of life: process ids get recycled, and
    # the new holder of the id knows nothing about this lock. The heartbeat is what
    # separates "same process, still working" from "same number, different process".
    if declared_ttl is not None and heartbeat_age_s is not None and heartbeat_age_s > declared_ttl:
        return LockStaleness(
            verdict=STALENESS_STALE,
            reason=(
                f"heartbeat is {_format_age(heartbeat_age_s)} old, past the "
                f"{_format_age(declared_ttl)} the owner promised to stay within"
            ),
            **{**base, "kind": STALE_KIND_HEARTBEAT_EXPIRED},
        )

    if pid is None:
        return LockStaleness(
            verdict=STALENESS_AMBIGUOUS,
            reason="lock records no usable owner pid",
            **base,
        )
    if process_running is None:
        return LockStaleness(
            verdict=STALENESS_AMBIGUOUS,
            reason=f"owner pid {pid} could not be probed on this host",
            **base,
        )
    if declared_ttl is None:
        return LockStaleness(
            verdict=STALENESS_ACTIVE,
            reason=(
                f"owner pid {pid} is running and the lock declares no heartbeat "
                "contract, so its age proves nothing"
            ),
            **base,
        )
    return LockStaleness(
        verdict=STALENESS_ACTIVE,
        reason=f"owner pid {pid} is running and its heartbeat is current",
        **base,
    )


def describe_run_mutation_lock(
    run_dir: Path,
    *,
    label: str = "",
) -> dict[str, Any] | None:
    """Public, token-free description of one lock plus its staleness verdict."""
    lock_path = run_dir / _LOCK_FILE_NAME
    metadata, raw_text = _load_metadata_with_text(lock_path)
    if metadata is None and raw_text is None:
        return None
    staleness = assess_lock_staleness(metadata)
    return {
        "label": label or run_dir.name,
        "lock_path": _safe_resolved_path(lock_path),
        "run_dir": _safe_resolved_path(run_dir),
        "readable": metadata is not None,
        "lock": _public_lock_metadata(metadata),
        "staleness": staleness.to_json(),
        "recovery_command": _unlock_command(metadata),
    }


def clear_run_mutation_lock(run_dir: Path, *, force: bool = False) -> dict[str, Any]:
    """Delete one lock file when it is provably stale (or when ``force``).

    Returns the same description :func:`describe_run_mutation_lock` produces, plus
    ``cleared`` and ``forced``. Never raises for a lock it declines to clear --
    refusing is a reportable outcome, not an error.
    """
    description = describe_run_mutation_lock(run_dir)
    if description is None:
        return {
            "label": run_dir.name,
            "lock_path": _safe_resolved_path(run_dir / _LOCK_FILE_NAME),
            "run_dir": _safe_resolved_path(run_dir),
            "present": False,
            "cleared": False,
            "forced": False,
        }
    verdict = str(description["staleness"]["verdict"])
    should_clear = force or verdict == STALENESS_STALE
    cleared = False
    if should_clear:
        with suppress(FileNotFoundError):
            (run_dir / _LOCK_FILE_NAME).unlink()
            cleared = True
        # A recovery claim outlives its lock only when the claiming process also
        # died; leaving it behind would block the next acquirer on a file the user
        # was just told they had cleared.
        with suppress(FileNotFoundError):
            (run_dir / _RECOVERY_FILE_NAME).unlink()
        _append_event(
            run_dir / _EVENTS_FILE_NAME,
            {
                "schema_version": LOCK_SCHEMA_VERSION,
                "event": "lock_cleared_by_operator",
                "reason_code": "forced_unlock"
                if force and verdict != STALENESS_STALE
                else "stale_lock_cleared",
                "forced": bool(force and verdict != STALENESS_STALE),
                "verdict": verdict,
                "cleared_by_pid": os.getpid(),
                "cleared_by_host": socket.gethostname(),
                "cleared_lock": description["lock"],
            },
        )
    return {
        **description,
        "present": True,
        "cleared": cleared,
        "forced": bool(force and verdict != STALENESS_STALE),
    }


def lock_is_live(run_dir: Path) -> bool:
    """True when a lock file exists and its owner is not provably gone.

    Deliberately conservative in the opposite direction from recovery: an
    ambiguous lock counts as live, because a caller asking "is something running
    here?" must not be told "no" on the strength of a question this host cannot
    answer.
    """
    metadata, raw_text = _load_metadata_with_text(run_dir / _LOCK_FILE_NAME)
    if metadata is None and raw_text is None:
        return False
    return not assess_lock_staleness(metadata).recoverable


def workspace_mutation_run_id(workspace_root: Path | str) -> str:
    return f"workspace:{normalize_workspace_identity_path(workspace_root)}"


def normalize_workspace_identity_path(workspace_root: Path | str) -> str:
    raw = os.fspath(workspace_root).strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.rstrip("/") or raw
    if _looks_like_windows_drive_path(raw):
        drive = raw[:2].lower()
        rest = "/".join(part for part in raw[2:].split("/") if part)
        return f"{drive}/{rest}".rstrip("/").casefold()
    if raw.startswith("/mnt/") and len(raw) >= 7 and raw[6:7] == "/":
        drive = raw[5:6].lower()
        tail = "/".join(part for part in raw[7:].split("/") if part)
        return f"{drive}:/{tail}".rstrip("/").casefold()
    try:
        return os.fspath(Path(raw).expanduser().resolve()).replace("\\", "/").rstrip("/")
    except OSError:
        return raw


def write_run_mutation_lock_metadata(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def lock_diagnostic(metadata: dict[str, Any] | None, *, run_id: str, mode: str) -> dict[str, Any]:
    error = _conflict_error(run_id=run_id, mode=mode, metadata=metadata)
    return {
        "reason_code": error.reason_code,
        "diagnostic": error.diagnostic,
        "blocked_by": _public_lock_metadata(metadata),
        "staleness": assess_lock_staleness(metadata).to_json(),
        "recovery_command": _unlock_command(metadata),
    }


def _owner_id(*, owner_token: str, owner_session_id: str | None) -> str:
    session = str(owner_session_id or "").strip()
    if session:
        return session
    return f"{socket.gethostname()}:{os.getpid()}:{owner_token[:12]}"


def _safe_resolved_path(path: Path | str) -> str:
    try:
        return os.fspath(Path(path).expanduser().resolve(strict=False)).replace("\\", "/")
    except (OSError, RuntimeError, ValueError):
        return os.fspath(path).replace("\\", "/")


def _looks_like_windows_drive_path(raw: str) -> bool:
    return len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":"


def _public_lock_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    public_keys = {
        "schema_version",
        "run_id",
        "mode",
        "kind",
        "pid",
        "hostname",
        "acquired_at",
        "started_at",
        "last_heartbeat_at",
        "owner_id",
        "owner_session_id",
        "workspace_root",
        "workspace_identity",
        "run_dir",
        "stale_policy",
        "reason_code",
        "diagnostic",
        "recovery_reason",
        "heartbeat_interval_s",
        "heartbeat_ttl_s",
    }
    return {key: value for key, value in metadata.items() if key in public_keys}


def _append_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event_payload = dict(payload)
    event_payload.pop("owner_token", None)
    event_payload.setdefault("event_at", now_iso())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event_payload, sort_keys=True) + "\n")


def _build_metadata(
    *,
    run_id: str,
    mode: str,
    workspace_root: Path,
    run_dir: Path,
    owner_token: str,
    owner_id: str,
    owner_session_id: str | None,
    kind: str,
    recovery_reason: str | None = None,
) -> dict[str, Any]:
    acquired_at = now_iso()
    payload: dict[str, Any] = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "kind": kind,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": acquired_at,
        "started_at": acquired_at,
        "last_heartbeat_at": acquired_at,
        "owner_id": owner_id,
        "owner_session_id": owner_session_id,
        "owner_token": owner_token,
        "workspace_root": _safe_resolved_path(workspace_root),
        "workspace_identity": normalize_workspace_identity_path(workspace_root),
        "run_dir": _safe_resolved_path(run_dir),
        "heartbeat_interval_s": heartbeat_interval_s(),
        "heartbeat_ttl_s": heartbeat_ttl_s(),
        "stale_policy": (
            "same-host owner with a dead pid or a heartbeat older than heartbeat_ttl_s "
            "is recoverable; other-host and unprobeable locks stay active"
        ),
        "reason_code": "stale_lock_recovery" if kind == "recovery" else "active_execution_lock",
        "diagnostic": (
            "Recovering a definitely stale Forge mutation lock."
            if kind == "recovery"
            else "Forge execution is mutating this workspace/run."
        ),
    }
    if recovery_reason:
        payload["recovery_reason"] = recovery_reason
    return payload


def _build_wait_metadata(
    *,
    run_id: str,
    mode: str,
    workspace_root: Path,
    run_dir: Path,
    owner_token: str,
    owner_id: str,
    owner_session_id: str | None,
    blocked_by: dict[str, Any],
    started_at: str,
    diagnostic: str,
) -> dict[str, Any]:
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "kind": "queued_wait",
        "reason_code": "blocked_by_active_workspace_execution",
        "run_id": run_id,
        "mode": mode,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": started_at,
        "last_heartbeat_at": started_at,
        "owner_id": owner_id,
        "owner_session_id": owner_session_id,
        "owner_token": owner_token,
        "workspace_root": _safe_resolved_path(workspace_root),
        "workspace_identity": normalize_workspace_identity_path(workspace_root),
        "run_dir": _safe_resolved_path(run_dir),
        "blocked_by": _public_lock_metadata(blocked_by),
        "stale_policy": "queued wait rechecks the lock and only recovers definitely stale locks",
        "diagnostic": diagnostic,
    }


def _metadata_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write_exclusive(path: Path, text: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(path, flags, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        with suppress(FileNotFoundError):
            path.unlink()
        raise


def _load_metadata(path: Path) -> dict[str, Any] | None:
    payload, _ = _load_metadata_with_text(path)
    return payload


def _load_metadata_with_text(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, text
    if not isinstance(payload, dict):
        return None, text
    return payload, text


def _clear_stale_recovery_claim(
    *,
    recovery_path: Path,
    run_id: str,
    mode: str,
) -> None:
    recovery_metadata = _load_metadata(recovery_path)
    if recovery_metadata is None:
        return
    if assess_lock_staleness(recovery_metadata).recoverable:
        with suppress(FileNotFoundError):
            recovery_path.unlink()
        return
    raise _conflict_error(
        run_id=run_id,
        mode=mode,
        metadata=recovery_metadata,
        note="another execution is already recovering this run",
    )


def _definitely_stale_reason(metadata: dict[str, Any]) -> str | None:
    """Back-compatible thin wrapper over :func:`assess_lock_staleness`."""
    staleness = assess_lock_staleness(metadata)
    return staleness.reason if staleness.recoverable else None


def _clean_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _coerce_pid(raw: Any) -> int | None:
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _coerce_positive_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _age_seconds(raw: Any, reference: datetime) -> float | None:
    text = _clean_str(raw)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Clamped at zero: a lock stamped by a host with a skewed clock must not read
    # as "negative age", which would print as nonsense and compare as fresh.
    return max(0.0, (reference - parsed).total_seconds())


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _recovery_grace_s(metadata: dict[str, Any]) -> float:
    declared = _coerce_positive_float(metadata.get("heartbeat_interval_s"))
    interval = declared if declared is not None else heartbeat_interval_s()
    return max(_MIN_HEARTBEAT_INTERVAL_S, min(interval, _MAX_RECOVERY_GRACE_S))


def _unlock_command(metadata: dict[str, Any] | None) -> str:
    workspace = _clean_str((metadata or {}).get("workspace_root"))
    if workspace is None:
        return f"{UNLOCK_COMMAND} --path <workspace>"
    return f'{UNLOCK_COMMAND} --path "{workspace}"'


def _process_is_running(pid: int) -> bool | None:
    if os.name == "nt":
        return _windows_process_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        if getattr(e, "errno", None) == errno.ESRCH:
            return False
        return None
    return True


def _windows_process_is_running(pid: int) -> bool | None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            error = ctypes.get_last_error()
            if error == _WINDOWS_ERROR_INVALID_PARAMETER:
                return False
            return None
        exit_code = wintypes.DWORD()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == _WINDOWS_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _conflict_error(
    *,
    run_id: str,
    mode: str,
    metadata: dict[str, Any] | None,
    note: str | None = None,
    reason_code: str = "blocked_by_active_workspace_execution",
) -> RunMutationConflictError:
    staleness = assess_lock_staleness(metadata)
    details: list[str] = []
    if metadata is not None:
        owner_mode = str(metadata.get("mode") or "").strip()
        if owner_mode:
            details.append(f"owner mode={owner_mode}")
        owner_id = str(metadata.get("owner_id") or "").strip()
        if owner_id:
            details.append(f"owner={owner_id}")
        pid = str(metadata.get("pid") or "").strip()
        if pid:
            details.append(f"pid={pid}")
        hostname = str(metadata.get("hostname") or "").strip()
        if hostname:
            details.append(f"host={hostname}")
        acquired_at = str(metadata.get("acquired_at") or "").strip()
        if acquired_at:
            details.append(f"acquired_at={acquired_at}")
        if staleness.age_s is not None:
            details.append(f"age={_format_age(staleness.age_s)}")
        if staleness.heartbeat_age_s is not None:
            details.append(f"heartbeat_age={_format_age(staleness.heartbeat_age_s)}")
    detail_suffix = f" ({', '.join(details)})" if details else ""
    note_suffix = f" {note}." if note else ""
    target = "workspace" if run_id.startswith("workspace:") else "run"
    # The verdict is what makes the guidance actionable: "wait" and "inspect it" are
    # different instructions, and the old message gave both at once for every case.
    verdict_suffix = (
        f" Lock verdict: {staleness.verdict} -- {staleness.reason}." if metadata else ""
    )
    message = (
        f"Another Forge execution is already mutating this {target} "
        f"(run_id={run_id}, requested_mode={mode}){detail_suffix}.{note_suffix}"
        f"{verdict_suffix} Wait for the active execution to finish, or inspect and clear the "
        f"lock with: {_unlock_command(metadata)}"
    )
    return RunMutationConflictError(
        message,
        reason_code=reason_code,
        metadata=_public_lock_metadata(metadata),
        diagnostic=message,
    )
