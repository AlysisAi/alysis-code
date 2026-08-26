"""Durable exactly-once acceptance for IDE Forge plan requests.

Only hashes of idempotency keys and request payloads are persisted.  The
instruction itself is deliberately never written to this database.  Workers
hold short, renewable, hashed leases so a stale process cannot publish a
result after ownership has moved.  An expired *running* lease is terminally
classified as indeterminate: callers must not automatically execute it again.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..branding import canonical_user_data_dir, env_get

SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 30.0
MIN_LEASE_SECONDS = 1.0
MAX_LEASE_SECONDS = 15 * 60.0
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


class ForgeRequestState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


TERMINAL_STATES = frozenset(
    {
        ForgeRequestState.COMPLETED,
        ForgeRequestState.FAILED,
        ForgeRequestState.CANCELLED,
        ForgeRequestState.INDETERMINATE,
    }
)


class ForgeRequestLedgerError(RuntimeError):
    """Base error whose message is safe to surface or log."""


class ForgeRequestValidationError(ForgeRequestLedgerError, ValueError):
    pass


class ForgeRequestIdempotencyConflict(ForgeRequestLedgerError):
    pass


class ForgeRequestNotFound(ForgeRequestLedgerError):
    pass


class ForgeRequestLeaseLost(ForgeRequestLedgerError):
    pass


class ForgeRequestStorageError(ForgeRequestLedgerError):
    pass


@dataclass(frozen=True, slots=True)
class ForgeRequestLedgerConfig:
    busy_timeout_seconds: float = 10.0
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    max_records: int = 50_000
    terminal_retention_seconds: float = 90 * 24 * 60 * 60.0

    def __post_init__(self) -> None:
        _duration(self.busy_timeout_seconds, maximum=60.0, label="busy timeout")
        _duration(self.lease_seconds, maximum=MAX_LEASE_SECONDS, label="lease")
        if isinstance(self.max_records, bool) or not isinstance(self.max_records, int):
            raise ForgeRequestValidationError("Forge request record limit must be an integer.")
        if self.max_records < 100 or self.max_records > 1_000_000:
            raise ForgeRequestValidationError(
                "Forge request record limit is outside the allowed range."
            )
        _duration(
            self.terminal_retention_seconds,
            maximum=10 * 365 * 24 * 60 * 60.0,
            label="terminal retention",
        )


@dataclass(frozen=True, slots=True)
class ForgeRequestRecord:
    job_id: str
    session_id: str
    workspace_root: Path = field(repr=False)
    state: ForgeRequestState
    created_at: float
    updated_at: float
    started_at: float | None = None
    terminal_at: float | None = None
    plan_id: str | None = None
    error_code: str | None = None
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class ForgeDispatchLease:
    job_id: str
    generation: int
    expires_at: float
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ForgeWorkerLease:
    job_id: str
    generation: int
    expires_at: float
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ForgeRequestAcceptance:
    record: ForgeRequestRecord
    created: bool
    dispatch_lease: ForgeDispatchLease | None = field(default=None, repr=False)


def default_forge_request_ledger_path() -> Path:
    """Return the private user-data path, outside workspaces by default."""

    override = str(env_get("ALYSIS_DATA_DIR") or "").strip()
    data_dir = Path(override).expanduser() if override else canonical_user_data_dir()
    return data_dir / "ide" / "forge-plan-requests.sqlite3"


class DurableForgeRequestLedger:
    """SQLite-backed Forge request acceptance and fenced worker ownership."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        config: ForgeRequestLedgerConfig | None = None,
        clock: Callable[[], float] = time.time,
        job_id_factory: Callable[[], str] | None = None,
        lease_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_forge_request_ledger_path()
        self.config = config or ForgeRequestLedgerConfig()
        self._clock = clock
        self._job_id_factory = job_id_factory or (lambda: f"job_{uuid.uuid4().hex}")
        self._lease_token_factory = lease_token_factory or (
            lambda: f"lease_{secrets.token_urlsafe(32)}"
        )
        self._lock = threading.RLock()
        self._prepare_storage()

    def accept(
        self,
        *,
        workspace_root: str | os.PathLike[str],
        session_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> ForgeRequestAcceptance:
        """Durably accept a request, or return its stable prior job.

        A stale queued dispatch can be safely fenced and redispatched because a
        worker must transition it to ``running`` before invoking the planner.
        A stale running request is never redispatched.
        """

        workspace = _workspace(workspace_root)
        session = _identifier(session_id, "session", maximum=256)
        key = _identifier(idempotency_key, "idempotency", maximum=256)
        workspace_key = _digest(os.path.normcase(os.fspath(workspace)))
        idempotency_hash = _digest(key)
        payload_hash = _payload_digest(payload)
        now = self._now()
        duration = self.config.lease_seconds

        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM ide_forge_plan_requests
                WHERE workspace_sha256 = ? AND idempotency_sha256 = ?
                """,
                (workspace_key, idempotency_hash),
            ).fetchone()
            if row is not None:
                if str(row["payload_sha256"]) != payload_hash:
                    raise ForgeRequestIdempotencyConflict(
                        "Idempotency key was already used for a different Forge plan request."
                    )
                row = self._reconcile_expired_running(connection, row=row, now=now)
                state = ForgeRequestState(str(row["state"]))
                connection.execute(
                    """
                    UPDATE ide_forge_plan_requests
                    SET session_id = ?, updated_at = CASE
                        WHEN session_id = ? THEN updated_at ELSE ? END
                    WHERE job_id = ?
                    """,
                    (session, session, now, row["job_id"]),
                )
                row = self._row_by_job(connection, str(row["job_id"]))
                if (
                    state is ForgeRequestState.QUEUED
                    and float(row["lease_expires_at"] or 0.0) <= now
                ):
                    lease = self._replace_dispatch_lease(connection, row=row, now=now)
                    return ForgeRequestAcceptance(
                        record=self._record(self._row_by_job(connection, lease.job_id)),
                        created=False,
                        dispatch_lease=lease,
                    )
                return ForgeRequestAcceptance(record=self._record(row), created=False)

            job_id = _identifier(self._job_id_factory(), "generated job", maximum=128)
            self._prune_for_insert(connection, now=now)
            token = _lease_token(self._lease_token_factory())
            generation = 1
            expires_at = now + duration
            try:
                connection.execute(
                    """
                    INSERT INTO ide_forge_plan_requests (
                        job_id, workspace_sha256, workspace_root, session_id,
                        idempotency_sha256, payload_sha256, state, generation,
                        attempts, lease_token_sha256, lease_expires_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        workspace_key,
                        os.fspath(workspace),
                        session,
                        idempotency_hash,
                        payload_hash,
                        generation,
                        _digest(token),
                        expires_at,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ForgeRequestStorageError(
                    "Could not allocate a durable Forge plan request."
                ) from None
            row = self._row_by_job(connection, job_id)
            return ForgeRequestAcceptance(
                record=self._record(row),
                created=True,
                dispatch_lease=ForgeDispatchLease(
                    job_id=job_id,
                    generation=generation,
                    expires_at=expires_at,
                    token=token,
                ),
            )

    def begin(self, lease: ForgeDispatchLease) -> ForgeWorkerLease:
        """Claim execution before the planner is invoked."""

        now = self._now()
        expires_at = now + self.config.lease_seconds
        with self._lock, self._transaction() as connection:
            row = self._require_lease(
                connection,
                job_id=lease.job_id,
                generation=lease.generation,
                token=lease.token,
                expected_state=ForgeRequestState.QUEUED,
                now=now,
            )
            connection.execute(
                """
                UPDATE ide_forge_plan_requests
                SET state = 'running', attempts = attempts + 1,
                    started_at = COALESCE(started_at, ?), updated_at = ?,
                    lease_expires_at = ?
                WHERE job_id = ?
                """,
                (now, now, expires_at, row["job_id"]),
            )
        return ForgeWorkerLease(
            job_id=lease.job_id,
            generation=lease.generation,
            expires_at=expires_at,
            token=lease.token,
        )

    def renew(self, lease: ForgeWorkerLease) -> ForgeWorkerLease:
        now = self._now()
        expires_at = now + self.config.lease_seconds
        with self._lock, self._transaction() as connection:
            self._require_lease(
                connection,
                job_id=lease.job_id,
                generation=lease.generation,
                token=lease.token,
                expected_state=ForgeRequestState.RUNNING,
                now=now,
            )
            connection.execute(
                """
                UPDATE ide_forge_plan_requests
                SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?
                """,
                (expires_at, now, lease.job_id),
            )
        return ForgeWorkerLease(
            job_id=lease.job_id,
            generation=lease.generation,
            expires_at=expires_at,
            token=lease.token,
        )

    def complete(self, lease: ForgeWorkerLease, *, plan_id: str) -> ForgeRequestRecord:
        clean_plan_id = _identifier(plan_id, "plan", maximum=128)
        return self._finish(
            lease,
            state=ForgeRequestState.COMPLETED,
            plan_id=clean_plan_id,
            error_code=None,
        )

    def fail(self, lease: ForgeWorkerLease, *, error_code: str) -> ForgeRequestRecord:
        return self._finish(
            lease,
            state=ForgeRequestState.FAILED,
            plan_id=None,
            error_code=_error_code(error_code),
        )

    def cancel(self, lease: ForgeWorkerLease, *, error_code: str) -> ForgeRequestRecord:
        return self._finish(
            lease,
            state=ForgeRequestState.CANCELLED,
            plan_id=None,
            error_code=_error_code(error_code),
        )

    def reject(self, lease: ForgeDispatchLease, *, error_code: str) -> ForgeRequestRecord:
        """Terminally reject accepted work that cannot be dispatched."""

        now = self._now()
        with self._lock, self._transaction() as connection:
            self._require_lease(
                connection,
                job_id=lease.job_id,
                generation=lease.generation,
                token=lease.token,
                expected_state=ForgeRequestState.QUEUED,
                now=now,
            )
            connection.execute(
                """
                UPDATE ide_forge_plan_requests
                SET state = 'failed', error_code = ?, terminal_at = ?, updated_at = ?,
                    lease_token_sha256 = NULL, lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (_error_code(error_code), now, now, lease.job_id),
            )
            return self._record(self._row_by_job(connection, lease.job_id))

    def get(self, job_id: str) -> ForgeRequestRecord | None:
        clean = _identifier(job_id, "job", maximum=128)
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ide_forge_plan_requests WHERE job_id = ?", (clean,)
            ).fetchone()
            if row is None:
                return None
            row = self._reconcile_expired_running(connection, row=row, now=now)
            return self._record(row)

    def attach(
        self,
        *,
        job_id: str,
        session_id: str,
        workspace_root: str | os.PathLike[str],
    ) -> ForgeRequestRecord:
        """Attach a recovered durable job to a live bridge session."""

        clean_job = _identifier(job_id, "job", maximum=128)
        clean_session = _identifier(session_id, "session", maximum=256)
        workspace = _workspace(workspace_root)
        workspace_key = _digest(os.path.normcase(os.fspath(workspace)))
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._row_by_job(connection, clean_job)
            if not secrets.compare_digest(str(row["workspace_sha256"]), workspace_key):
                raise ForgeRequestValidationError(
                    "Durable Forge plan request workspace binding does not match."
                )
            connection.execute(
                """
                UPDATE ide_forge_plan_requests SET session_id = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (clean_session, now, clean_job),
            )
            return self._record(self._row_by_job(connection, clean_job))

    def _finish(
        self,
        lease: ForgeWorkerLease,
        *,
        state: ForgeRequestState,
        plan_id: str | None,
        error_code: str | None,
    ) -> ForgeRequestRecord:
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._require_lease(
                connection,
                job_id=lease.job_id,
                generation=lease.generation,
                token=lease.token,
                expected_state=ForgeRequestState.RUNNING,
                now=now,
            )
            connection.execute(
                """
                UPDATE ide_forge_plan_requests
                SET state = ?, plan_id = ?, error_code = ?, terminal_at = ?,
                    updated_at = ?, lease_token_sha256 = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (state.value, plan_id, error_code, now, now, lease.job_id),
            )
            return self._record(self._row_by_job(connection, lease.job_id))

    def _replace_dispatch_lease(
        self, connection: sqlite3.Connection, *, row: sqlite3.Row, now: float
    ) -> ForgeDispatchLease:
        token = _lease_token(self._lease_token_factory())
        generation = int(row["generation"]) + 1
        expires_at = now + self.config.lease_seconds
        connection.execute(
            """
            UPDATE ide_forge_plan_requests
            SET generation = ?, lease_token_sha256 = ?, lease_expires_at = ?,
                updated_at = ? WHERE job_id = ? AND state = 'queued'
            """,
            (generation, _digest(token), expires_at, now, row["job_id"]),
        )
        return ForgeDispatchLease(
            job_id=str(row["job_id"]),
            generation=generation,
            expires_at=expires_at,
            token=token,
        )

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        generation: int,
        token: str,
        expected_state: ForgeRequestState,
        now: float,
    ) -> sqlite3.Row:
        row = self._row_by_job(connection, _identifier(job_id, "job", maximum=128))
        if (
            str(row["state"]) != expected_state.value
            or int(row["generation"]) != generation
            or not secrets.compare_digest(str(row["lease_token_sha256"] or ""), _digest(token))
            or float(row["lease_expires_at"] or 0.0) <= now
        ):
            raise ForgeRequestLeaseLost("Forge plan worker lease is no longer valid.")
        return row

    def _reconcile_expired_running(
        self, connection: sqlite3.Connection, *, row: sqlite3.Row, now: float
    ) -> sqlite3.Row:
        if str(row["state"]) != ForgeRequestState.RUNNING.value:
            return row
        if float(row["lease_expires_at"] or 0.0) > now:
            return row
        connection.execute(
            """
            UPDATE ide_forge_plan_requests
            SET state = 'indeterminate', error_code = 'worker_lease_expired',
                terminal_at = ?, updated_at = ?, lease_token_sha256 = NULL,
                lease_expires_at = NULL
            WHERE job_id = ? AND state = 'running' AND lease_expires_at <= ?
            """,
            (now, now, row["job_id"], now),
        )
        return self._row_by_job(connection, str(row["job_id"]))

    def _prune_for_insert(self, connection: sqlite3.Connection, *, now: float) -> None:
        cutoff = now - self.config.terminal_retention_seconds
        connection.execute(
            """
            DELETE FROM ide_forge_plan_requests
            WHERE state IN ('completed', 'failed', 'cancelled', 'indeterminate')
              AND terminal_at < ?
            """,
            (cutoff,),
        )
        count = int(
            connection.execute("SELECT COUNT(*) FROM ide_forge_plan_requests").fetchone()[0]
        )
        overflow = count - self.config.max_records + 1
        if overflow > 0:
            connection.execute(
                """
                DELETE FROM ide_forge_plan_requests WHERE job_id IN (
                    SELECT job_id FROM ide_forge_plan_requests
                    WHERE state IN ('completed', 'failed', 'cancelled', 'indeterminate')
                    ORDER BY terminal_at ASC LIMIT ?
                )
                """,
                (overflow,),
            )
            count = int(
                connection.execute("SELECT COUNT(*) FROM ide_forge_plan_requests").fetchone()[0]
            )
        if count >= self.config.max_records:
            raise ForgeRequestStorageError("Durable Forge plan request storage is full.")

    def _record(self, row: sqlite3.Row) -> ForgeRequestRecord:
        return ForgeRequestRecord(
            job_id=str(row["job_id"]),
            session_id=str(row["session_id"]),
            workspace_root=Path(str(row["workspace_root"])),
            state=ForgeRequestState(str(row["state"])),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=(None if row["started_at"] is None else float(row["started_at"])),
            terminal_at=(None if row["terminal_at"] is None else float(row["terminal_at"])),
            plan_id=(None if row["plan_id"] is None else str(row["plan_id"])),
            error_code=(None if row["error_code"] is None else str(row["error_code"])),
            attempts=int(row["attempts"]),
        )

    def _row_by_job(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM ide_forge_plan_requests WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ForgeRequestNotFound("Durable Forge plan request was not found.")
        return row

    def _prepare_storage(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with suppress(OSError):
                self.path.parent.chmod(0o700)
            with self._connection() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, SCHEMA_VERSION}:
                    raise ForgeRequestStorageError(
                        "Durable Forge plan request storage schema is unsupported."
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ide_forge_plan_requests (
                        job_id TEXT PRIMARY KEY,
                        workspace_sha256 TEXT NOT NULL,
                        workspace_root TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        idempotency_sha256 TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('queued', 'running', 'completed', 'failed',
                                      'cancelled', 'indeterminate')
                        ),
                        generation INTEGER NOT NULL DEFAULT 1,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        lease_token_sha256 TEXT,
                        lease_expires_at REAL,
                        plan_id TEXT,
                        error_code TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        terminal_at REAL,
                        UNIQUE(workspace_sha256, idempotency_sha256)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ide_forge_plan_requests_state "
                    "ON ide_forge_plan_requests(state, lease_expires_at)"
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            with suppress(OSError):
                self.path.chmod(0o600)
        except (OSError, sqlite3.Error) as exc:
            raise ForgeRequestStorageError(
                "Could not prepare durable Forge plan request storage."
            ) from exc

    @contextmanager
    def _connection(self):
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.config.busy_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.config.busy_timeout_seconds * 1000)}"
            )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        except ForgeRequestLedgerError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ForgeRequestStorageError("Durable Forge plan request storage failed.") from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _now(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            raise ForgeRequestStorageError("Forge request ledger clock failed.") from None
        if not math.isfinite(value) or value < 0:
            raise ForgeRequestStorageError("Forge request ledger clock failed.")
        return value


def _workspace(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ForgeRequestValidationError("Forge request workspace is invalid.") from None
    if not path.is_dir():
        raise ForgeRequestValidationError("Forge request workspace must be a directory.")
    return path


def _identifier(value: Any, label: str, *, maximum: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > maximum or _IDENTIFIER_RE.fullmatch(clean) is None:
        raise ForgeRequestValidationError(f"Forge request {label} identifier is invalid.")
    return clean


def _lease_token(value: Any) -> str:
    return _identifier(value, "lease", maximum=256)


def _error_code(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 128 or _ERROR_CODE_RE.fullmatch(clean) is None:
        raise ForgeRequestValidationError("Forge request error code is invalid.")
    return clean


def _duration(value: Any, *, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ForgeRequestValidationError(f"Forge request {label} must be numeric.")
    clean = float(value)
    if not math.isfinite(clean) or clean < MIN_LEASE_SECONDS or clean > maximum:
        raise ForgeRequestValidationError(f"Forge request {label} is outside the allowed range.")
    return clean


def _payload_digest(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ForgeRequestValidationError("Forge request payload is not valid JSON.") from None
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DurableForgeRequestLedger",
    "ForgeDispatchLease",
    "ForgeRequestAcceptance",
    "ForgeRequestIdempotencyConflict",
    "ForgeRequestLeaseLost",
    "ForgeRequestLedgerConfig",
    "ForgeRequestLedgerError",
    "ForgeRequestNotFound",
    "ForgeRequestRecord",
    "ForgeRequestState",
    "ForgeRequestStorageError",
    "ForgeRequestValidationError",
    "ForgeWorkerLease",
    "default_forge_request_ledger_path",
]
