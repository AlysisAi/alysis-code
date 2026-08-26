"""Durable, fenced coordination for resumable IDE swarm executions.

This module deliberately does not start threads or import the Forge runner.  It
is the persistence and authority boundary a bridge can place around any
background executor:

1. create a job with a non-secret execution specification and permission scope,
2. claim it and hand the returned :class:`SwarmWorkerLease` to one worker,
3. renew the lease while executing and record bounded usage,
4. complete, interrupt, or fail using that same lease, and
5. after a crash, explicitly resume an expired/interrupted job only if the
   freshly evaluated permission scope still has the same fingerprint.

SQLite ``BEGIN IMMEDIATE`` transactions serialize cross-process state changes.
Worker and owner authority values are stored only as SHA-256 digests; public
records never contain a lease token, worker identity, idempotency key, execution
specification, or permission descriptor.
"""

from __future__ import annotations

import hashlib
import hmac
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
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeAlias

from ..branding import canonical_user_data_dir, env_get

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 120.0
MIN_LEASE_SECONDS = 1.0
MAX_LEASE_SECONDS = 24.0 * 60.0 * 60.0
MAX_USAGE_VALUE = 9_000_000_000_000_000_000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret|credential)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SECRET_TOKEN_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|gh[opusr]_[A-Za-z0-9]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})"
)
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "authorization",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "lease_token",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_auth_token",
    "_refresh_token",
    "_client_secret",
    "_credential",
    "_credentials",
    "_lease_token",
    "_password",
    "_private_key",
    "_secret",
)
_SENSITIVE_KEY_PREFIXES = (
    "api_key_",
    "access_token_",
    "auth_token_",
    "client_secret_",
    "cookie_",
    "credential_",
    "lease_token_",
    "password_",
    "private_key_",
    "refresh_token_",
)
_REDACTED = "[REDACTED]"


class SwarmJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {SwarmJobState.SUCCEEDED, SwarmJobState.FAILED, SwarmJobState.CANCELLED}
)


class ResumableSwarmError(RuntimeError):
    """Base error whose message is safe to surface to an IDE client."""


class SwarmValidationError(ResumableSwarmError, ValueError):
    pass


class SwarmStorageError(ResumableSwarmError):
    pass


class SwarmJobNotFound(ResumableSwarmError):
    pass


class SwarmJobStateError(ResumableSwarmError):
    pass


class SwarmLeaseLost(ResumableSwarmError):
    pass


class SwarmPermissionScopeChanged(ResumableSwarmError):
    pass


class SwarmFreshPermissionGrantRequired(ResumableSwarmError):
    pass


class SwarmRevisionConflict(ResumableSwarmError):
    def __init__(self, current_revision: int) -> None:
        super().__init__("Swarm job changed; reload it before updating.")
        self.current_revision = current_revision


class SwarmIdempotencyConflict(ResumableSwarmError):
    pass


class SwarmCapacityError(ResumableSwarmError):
    pass


@dataclass(frozen=True, slots=True)
class ResumableSwarmConfig:
    max_execution_spec_bytes: int = 256 * 1024
    max_result_bytes: int = 1024 * 1024
    max_json_depth: int = 20
    max_json_nodes: int = 20_000
    max_string_chars: int = 16_384
    max_error_summary_chars: int = 600
    max_outstanding_per_session: int = 100
    max_list_limit: int = 200
    busy_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        integers = (
            self.max_execution_spec_bytes,
            self.max_result_bytes,
            self.max_json_depth,
            self.max_json_nodes,
            self.max_string_chars,
            self.max_error_summary_chars,
            self.max_outstanding_per_session,
            self.max_list_limit,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise SwarmValidationError("Resumable swarm limits must be positive integers.")
        if self.max_error_summary_chars < 4:
            raise SwarmValidationError("Resumable swarm error summary limit is too small.")
        if (
            self.max_execution_spec_bytes > 4 * 1024 * 1024
            or self.max_result_bytes > 8 * 1024 * 1024
            or self.max_json_depth > 64
            or self.max_json_nodes > 200_000
            or self.max_string_chars > 256 * 1024
            or self.max_error_summary_chars > 4_096
            or self.max_outstanding_per_session > 10_000
            or self.max_list_limit > 1_000
        ):
            raise SwarmValidationError("Resumable swarm limits exceed safe bounds.")
        timeout = self.busy_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise SwarmValidationError("Resumable swarm busy timeout is invalid.")


@dataclass(frozen=True, slots=True)
class SwarmUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0

    def public_payload(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class SwarmJobStatus:
    job_id: str
    state: SwarmJobState
    revision: int
    attempts: int
    resume_count: int
    created_at: float
    updated_at: float
    started_at: float | None
    terminal_at: float | None
    lease_expires_at: float | None
    result_available: bool
    error_code: str | None
    error_summary: str | None
    usage: SwarmUsage

    def public_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "revision": self.revision,
            "attempts": self.attempts,
            "resume_count": self.resume_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "terminal_at": self.terminal_at,
            "lease_expires_at": self.lease_expires_at,
            "result_available": self.result_available,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "usage": self.usage.public_payload(),
            "resumable": self.state in {SwarmJobState.INTERRUPTED, SwarmJobState.FAILED},
        }


@dataclass(frozen=True, slots=True)
class SwarmJobResult:
    status: SwarmJobStatus
    result: dict[str, JsonValue] | None

    def public_payload(self) -> dict[str, Any]:
        return {**self.status.public_payload(), "result": self.result}


@dataclass(frozen=True, slots=True)
class SwarmJobCreation:
    status: SwarmJobStatus
    created: bool


@dataclass(frozen=True, slots=True)
class SwarmWorkerLease:
    """Private worker authority; never serialize or return from a bridge method."""

    job_id: str
    session_id: str
    generation: int
    expires_at: float
    execution_spec: dict[str, JsonValue] = field(repr=False)
    lease_token: str = field(repr=False)


def default_resumable_swarm_path() -> Path:
    override = str(env_get("ALYSIS_DATA_DIR") or "").strip()
    data_dir = Path(override).expanduser() if override else canonical_user_data_dir()
    return data_dir / "ide" / "resumable-swarm.sqlite3"


class DurableResumableSwarmCoordinator:
    """Owner/workspace-scoped durable job coordinator.

    The caller remains responsible for running the actual worker and stopping
    its process tree.  This class fences all durable acceptance so a cancelled,
    expired, or superseded worker cannot publish usage or a final result.
    """

    def __init__(
        self,
        *,
        owner_id: str,
        workspace_root: str | os.PathLike[str],
        path: str | os.PathLike[str] | None = None,
        config: ResumableSwarmConfig | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        lease_factory: Callable[[], str] | None = None,
    ) -> None:
        owner_id = _identifier(owner_id, "owner", maximum=256)
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise SwarmValidationError("Resumable swarm workspace path is invalid.") from None
        if not workspace.is_dir():
            raise SwarmValidationError("Resumable swarm workspace must be a directory.")
        try:
            storage = (
                Path(path).expanduser() if path is not None else default_resumable_swarm_path()
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            raise SwarmValidationError("Resumable swarm storage path is invalid.") from None
        self.workspace_root = workspace
        self.path = storage
        self.config = config or ResumableSwarmConfig()
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lease_factory = lease_factory or (lambda: f"lease_{secrets.token_urlsafe(32)}")
        self._owner_key = _digest(owner_id)
        self._workspace_key = _digest(os.path.normcase(os.fspath(workspace)))
        self._lock = threading.RLock()
        self._validate_storage_location()
        self._prepare_storage()

    def start_job(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        execution_spec: Mapping[str, Any],
        permission_scope: Mapping[str, Any],
    ) -> SwarmJobCreation:
        """Create a queued job or return the identical prior request."""

        session_id = _identifier(session_id, "session", maximum=256)
        idempotency_key = _identifier(idempotency_key, "idempotency", maximum=256)
        spec_json, _ = _serialize_json_object(
            execution_spec,
            config=self.config,
            maximum_bytes=self.config.max_execution_spec_bytes,
            label="execution specification",
            redact=True,
        )
        spec_hash = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
        permission_hash = _permission_fingerprint(permission_scope, self.config)
        idempotency_hash = _digest(idempotency_key)
        now = self._now()
        with self._lock, self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM ide_swarm_jobs
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                    AND idempotency_hash = ?
                """,
                (self._owner_key, self._workspace_key, session_id, idempotency_hash),
            ).fetchone()
            if existing is not None:
                if (
                    existing["execution_spec_sha256"] != spec_hash
                    or existing["permission_scope_sha256"] != permission_hash
                ):
                    raise SwarmIdempotencyConflict(
                        "Idempotency key was already used for a different swarm job."
                    )
                return SwarmJobCreation(status=self._status(existing), created=False)
            outstanding = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM ide_swarm_jobs
                    WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                        AND state IN ('queued', 'running', 'interrupted')
                    """,
                    (self._owner_key, self._workspace_key, session_id),
                ).fetchone()[0]
            )
            if outstanding >= self.config.max_outstanding_per_session:
                raise SwarmCapacityError("Session has too many outstanding swarm jobs.")
            job_id = _identifier(self._id_factory(), "generated job", maximum=128)
            try:
                connection.execute(
                    """
                    INSERT INTO ide_swarm_jobs (
                        owner_key, workspace_key, session_id, job_id, idempotency_hash,
                        execution_spec_json, execution_spec_sha256,
                        permission_scope_sha256, state, revision, generation, attempts,
                        resume_count, created_at, updated_at, usage_calls,
                        usage_input_tokens, usage_output_tokens,
                        usage_cached_input_tokens, usage_total_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 1, 0, 0, 0, ?, ?, 0, 0, 0, 0, 0)
                    """,
                    (
                        self._owner_key,
                        self._workspace_key,
                        session_id,
                        job_id,
                        idempotency_hash,
                        spec_json,
                        spec_hash,
                        permission_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise SwarmStorageError("Could not allocate a unique swarm job.") from None
            row = self._job_row(connection, session_id=session_id, job_id=job_id)
            return SwarmJobCreation(status=self._status(row), created=True)

    def claim_job(
        self,
        *,
        session_id: str,
        job_id: str,
        worker_id: str,
        permission_scope: Mapping[str, Any],
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> SwarmWorkerLease:
        """Claim one queued job after rechecking its current permission scope."""

        session_id, job_id = _job_scope(session_id, job_id)
        worker_id = _identifier(worker_id, "worker", maximum=256)
        duration = _duration(lease_seconds)
        permission_hash = _permission_fingerprint(permission_scope, self.config)
        now = self._now()
        expires_at = _deadline(now, duration)
        token = _lease_token(self._lease_factory())
        token_hash = _digest(token)
        with self._lock, self._transaction() as connection:
            row = self._job_row(connection, session_id=session_id, job_id=job_id)
            _require_permission(row, permission_hash)
            if row["state"] != SwarmJobState.QUEUED.value:
                raise SwarmJobStateError("Swarm job is not queued for execution.")
            generation = int(row["generation"]) + 1
            connection.execute(
                """
                UPDATE ide_swarm_jobs
                SET state = 'running', revision = revision + 1, generation = ?,
                    attempts = attempts + 1, updated_at = ?,
                    started_at = COALESCE(started_at, ?), lease_token_sha256 = ?,
                    lease_worker_sha256 = ?, lease_expires_at = ?,
                    terminal_at = NULL, error_code = NULL, error_summary = NULL,
                    result_json = NULL
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
                """,
                (
                    generation,
                    now,
                    now,
                    token_hash,
                    _digest(worker_id),
                    expires_at,
                    self._owner_key,
                    self._workspace_key,
                    session_id,
                    job_id,
                ),
            )
            spec = _decode_stored_object(
                row["execution_spec_json"],
                label="execution specification",
                config=self.config,
                maximum_bytes=self.config.max_execution_spec_bytes,
            )
            return SwarmWorkerLease(
                job_id=job_id,
                session_id=session_id,
                generation=generation,
                expires_at=expires_at,
                execution_spec=spec,
                lease_token=token,
            )

    def renew(
        self,
        lease: SwarmWorkerLease,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> SwarmWorkerLease:
        duration = _duration(lease_seconds)
        now = self._now()
        expires_at = _deadline(now, duration)
        with self._lock, self._transaction() as connection:
            row = self._require_live_lease(connection, lease=lease, now=now)
            connection.execute(
                """
                UPDATE ide_swarm_jobs SET lease_expires_at = ?, updated_at = ?,
                    revision = revision + 1
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
                """,
                (
                    expires_at,
                    now,
                    self._owner_key,
                    self._workspace_key,
                    lease.session_id,
                    lease.job_id,
                ),
            )
            return SwarmWorkerLease(
                job_id=lease.job_id,
                session_id=lease.session_id,
                generation=lease.generation,
                expires_at=expires_at,
                execution_spec=_decode_stored_object(
                    row["execution_spec_json"],
                    label="execution specification",
                    config=self.config,
                    maximum_bytes=self.config.max_execution_spec_bytes,
                ),
                lease_token=lease.lease_token,
            )

    def record_usage(
        self,
        lease: SwarmWorkerLease,
        usage: SwarmUsage,
        *,
        idempotency_key: str,
    ) -> SwarmJobStatus:
        """Add one exactly-once usage event while the worker lease is live."""

        delta = _validated_usage(usage)
        event_hash = _digest(_identifier(idempotency_key, "usage event", maximum=256))
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._require_live_lease(connection, lease=lease, now=now)
            prior = connection.execute(
                """
                SELECT * FROM ide_swarm_usage_events
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                    AND job_id = ? AND event_hash = ?
                """,
                (
                    self._owner_key,
                    self._workspace_key,
                    lease.session_id,
                    lease.job_id,
                    event_hash,
                ),
            ).fetchone()
            if prior is not None:
                prior_usage = SwarmUsage(
                    calls=_stored_nonnegative_int(prior["calls"]),
                    input_tokens=_stored_nonnegative_int(prior["input_tokens"]),
                    output_tokens=_stored_nonnegative_int(prior["output_tokens"]),
                    cached_input_tokens=_stored_nonnegative_int(prior["cached_input_tokens"]),
                    total_tokens=_stored_nonnegative_int(prior["total_tokens"]),
                )
                if prior_usage != delta:
                    raise SwarmIdempotencyConflict(
                        "Usage idempotency key was already used for a different event."
                    )
                return self._status(row)
            current = _usage_from_row(row)
            combined = SwarmUsage(
                calls=_safe_usage_sum(current.calls, delta.calls),
                input_tokens=_safe_usage_sum(current.input_tokens, delta.input_tokens),
                output_tokens=_safe_usage_sum(current.output_tokens, delta.output_tokens),
                cached_input_tokens=_safe_usage_sum(
                    current.cached_input_tokens, delta.cached_input_tokens
                ),
                total_tokens=_safe_usage_sum(current.total_tokens, delta.total_tokens),
            )
            connection.execute(
                """
                INSERT INTO ide_swarm_usage_events (
                    owner_key, workspace_key, session_id, job_id, event_hash,
                    calls, input_tokens, output_tokens, cached_input_tokens,
                    total_tokens, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._owner_key,
                    self._workspace_key,
                    lease.session_id,
                    lease.job_id,
                    event_hash,
                    delta.calls,
                    delta.input_tokens,
                    delta.output_tokens,
                    delta.cached_input_tokens,
                    delta.total_tokens,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE ide_swarm_jobs
                SET usage_calls = ?, usage_input_tokens = ?, usage_output_tokens = ?,
                    usage_cached_input_tokens = ?, usage_total_tokens = ?, updated_at = ?,
                    revision = revision + 1
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
                """,
                (
                    combined.calls,
                    combined.input_tokens,
                    combined.output_tokens,
                    combined.cached_input_tokens,
                    combined.total_tokens,
                    now,
                    self._owner_key,
                    self._workspace_key,
                    lease.session_id,
                    lease.job_id,
                ),
            )
            return self._status(
                self._job_row(connection, session_id=lease.session_id, job_id=lease.job_id)
            )

    def complete(
        self,
        lease: SwarmWorkerLease,
        *,
        result: Mapping[str, Any],
    ) -> SwarmJobResult:
        result_json, normalized = _serialize_json_object(
            result,
            config=self.config,
            maximum_bytes=self.config.max_result_bytes,
            label="swarm result",
            redact=True,
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._require_live_lease(connection, lease=lease, now=now)
            self._finish_locked(
                connection,
                lease=lease,
                state=SwarmJobState.SUCCEEDED,
                now=now,
                result_json=result_json,
                error_code=None,
                error_summary=None,
            )
            status = self._status(
                self._job_row(connection, session_id=lease.session_id, job_id=lease.job_id)
            )
            return SwarmJobResult(status=status, result=normalized)

    def interrupt(
        self,
        lease: SwarmWorkerLease,
        *,
        error_code: str = "worker_interrupted",
        error_summary: str = "The swarm worker was interrupted.",
    ) -> SwarmJobStatus:
        code = _error_code(error_code)
        summary = _public_text(error_summary, maximum=self.config.max_error_summary_chars)
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._require_live_lease(connection, lease=lease, now=now)
            self._finish_locked(
                connection,
                lease=lease,
                state=SwarmJobState.INTERRUPTED,
                now=now,
                result_json=None,
                error_code=code,
                error_summary=summary,
            )
            return self._status(
                self._job_row(connection, session_id=lease.session_id, job_id=lease.job_id)
            )

    def fail(
        self,
        lease: SwarmWorkerLease,
        *,
        error_code: str,
        error_summary: str,
    ) -> SwarmJobStatus:
        code = _error_code(error_code)
        summary = _public_text(error_summary, maximum=self.config.max_error_summary_chars)
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._require_live_lease(connection, lease=lease, now=now)
            self._finish_locked(
                connection,
                lease=lease,
                state=SwarmJobState.FAILED,
                now=now,
                result_json=None,
                error_code=code,
                error_summary=summary,
            )
            return self._status(
                self._job_row(connection, session_id=lease.session_id, job_id=lease.job_id)
            )

    def cancel_job(
        self,
        *,
        session_id: str,
        job_id: str,
        expected_revision: int | None = None,
    ) -> SwarmJobStatus:
        """Fence the active worker before reporting a job as cancelled."""

        session_id, job_id = _job_scope(session_id, job_id)
        revision = _optional_revision(expected_revision)
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._job_row(connection, session_id=session_id, job_id=job_id)
            _check_revision(row, revision)
            state = SwarmJobState(row["state"])
            if state is SwarmJobState.CANCELLED:
                return self._status(row)
            if state in {SwarmJobState.SUCCEEDED, SwarmJobState.FAILED}:
                raise SwarmJobStateError("Completed swarm job cannot be cancelled.")
            connection.execute(
                """
                UPDATE ide_swarm_jobs
                SET state = 'cancelled', revision = revision + 1,
                    generation = generation + 1, updated_at = ?, terminal_at = ?,
                    lease_token_sha256 = NULL, lease_worker_sha256 = NULL,
                    lease_expires_at = NULL, result_json = NULL,
                    error_code = 'cancelled', error_summary = 'The swarm job was cancelled.'
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
                """,
                (now, now, self._owner_key, self._workspace_key, session_id, job_id),
            )
            return self._status(self._job_row(connection, session_id=session_id, job_id=job_id))

    def recover_stale_jobs(self, *, session_id: str | None = None) -> tuple[SwarmJobStatus, ...]:
        """Fence expired workers and mark their jobs interrupted.

        Recovery never silently requeues work.  The caller must invoke
        :meth:`resume_job`, which performs a fresh permission-scope check.
        """

        normalized_session = (
            None if session_id is None else _identifier(session_id, "session", maximum=256)
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            clauses = ["owner_key = ?", "workspace_key = ?", "state = 'running'"]
            values: list[Any] = [self._owner_key, self._workspace_key]
            if normalized_session is not None:
                clauses.append("session_id = ?")
                values.append(normalized_session)
            clauses.append("lease_expires_at <= ?")
            values.append(now)
            rows = connection.execute(
                f"SELECT session_id, job_id FROM ide_swarm_jobs WHERE {' AND '.join(clauses)}",  # noqa: S608 - clauses are fixed literals
                values,
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE ide_swarm_jobs
                    SET state = 'interrupted', revision = revision + 1,
                        generation = generation + 1, updated_at = ?,
                        lease_token_sha256 = NULL, lease_worker_sha256 = NULL,
                        lease_expires_at = NULL, result_json = NULL,
                        error_code = 'worker_lease_expired',
                        error_summary = 'The prior swarm worker lease expired.'
                    WHERE owner_key = ? AND workspace_key = ?
                        AND session_id = ? AND job_id = ? AND state = 'running'
                        AND lease_expires_at <= ?
                    """,
                    (
                        now,
                        self._owner_key,
                        self._workspace_key,
                        row["session_id"],
                        row["job_id"],
                        now,
                    ),
                )
            return tuple(
                self._status(
                    self._job_row(
                        connection, session_id=str(row["session_id"]), job_id=str(row["job_id"])
                    )
                )
                for row in rows
            )

    def resume_job(
        self,
        *,
        session_id: str,
        job_id: str,
        permission_scope: Mapping[str, Any],
        fresh_permission_grant: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> SwarmJobStatus:
        """Explicitly requeue interrupted/failed work after a fresh scope check."""

        session_id, job_id = _job_scope(session_id, job_id)
        revision = _optional_revision(expected_revision)
        permission_hash = _permission_fingerprint(permission_scope, self.config)
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._job_row(connection, session_id=session_id, job_id=job_id)
            _check_revision(row, revision)
            _require_fresh_permission_grant(
                fresh_permission_grant,
                row=row,
                session_id=session_id,
                job_id=job_id,
            )
            if (
                row["state"] == SwarmJobState.RUNNING.value
                and row["lease_expires_at"] is not None
                and float(row["lease_expires_at"]) <= now
            ):
                connection.execute(
                    """
                    UPDATE ide_swarm_jobs
                    SET state = 'interrupted', revision = revision + 1,
                        generation = generation + 1, updated_at = ?,
                        lease_token_sha256 = NULL, lease_worker_sha256 = NULL,
                        lease_expires_at = NULL, result_json = NULL,
                        error_code = 'worker_lease_expired',
                        error_summary = 'The prior swarm worker lease expired.'
                    WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
                    """,
                    (now, self._owner_key, self._workspace_key, session_id, job_id),
                )
                row = self._job_row(connection, session_id=session_id, job_id=job_id)
            _require_permission(row, permission_hash)
            if row["state"] not in {
                SwarmJobState.INTERRUPTED.value,
                SwarmJobState.FAILED.value,
            }:
                raise SwarmJobStateError("Swarm job is not in a resumable state.")
            connection.execute(
                """
                UPDATE ide_swarm_jobs
                SET state = 'queued', revision = revision + 1,
                    generation = generation + 1, resume_count = resume_count + 1,
                    updated_at = ?, terminal_at = NULL,
                    lease_token_sha256 = NULL, lease_worker_sha256 = NULL,
                    lease_expires_at = NULL, result_json = NULL,
                    error_code = NULL, error_summary = NULL
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
                """,
                (now, self._owner_key, self._workspace_key, session_id, job_id),
            )
            return self._status(self._job_row(connection, session_id=session_id, job_id=job_id))

    def should_cancel(self, lease: SwarmWorkerLease) -> bool:
        """Return true when a worker no longer owns live result authority."""

        try:
            now = self._now()
            with self._connection() as connection:
                self._require_live_lease(connection, lease=lease, now=now)
            return False
        except (SwarmJobNotFound, SwarmLeaseLost):
            return True

    def get_status(self, *, session_id: str, job_id: str) -> SwarmJobStatus:
        session_id, job_id = _job_scope(session_id, job_id)
        with self._connection() as connection:
            return self._status(self._job_row(connection, session_id=session_id, job_id=job_id))

    def get_result(self, *, session_id: str, job_id: str) -> SwarmJobResult:
        session_id, job_id = _job_scope(session_id, job_id)
        with self._connection() as connection:
            row = self._job_row(connection, session_id=session_id, job_id=job_id)
            result = (
                None
                if row["result_json"] is None
                else _decode_stored_object(
                    row["result_json"],
                    label="swarm result",
                    config=self.config,
                    maximum_bytes=self.config.max_result_bytes,
                )
            )
            return SwarmJobResult(status=self._status(row), result=result)

    def list_jobs(
        self,
        *,
        session_id: str,
        limit: int = 100,
    ) -> tuple[SwarmJobStatus, ...]:
        session_id = _identifier(session_id, "session", maximum=256)
        limit = _limit(limit, maximum=self.config.max_list_limit)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ide_swarm_jobs
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (self._owner_key, self._workspace_key, session_id, limit),
            ).fetchall()
            return tuple(self._status(row) for row in rows)

    def _status(self, row: sqlite3.Row) -> SwarmJobStatus:
        return _status_from_row(
            row,
            maximum_error_chars=self.config.max_error_summary_chars,
        )

    def _finish_locked(
        self,
        connection: sqlite3.Connection,
        *,
        lease: SwarmWorkerLease,
        state: SwarmJobState,
        now: float,
        result_json: str | None,
        error_code: str | None,
        error_summary: str | None,
    ) -> None:
        terminal_at = now if state in TERMINAL_STATES else None
        connection.execute(
            """
            UPDATE ide_swarm_jobs
            SET state = ?, revision = revision + 1, generation = generation + 1,
                updated_at = ?, terminal_at = ?, lease_token_sha256 = NULL,
                lease_worker_sha256 = NULL, lease_expires_at = NULL,
                result_json = ?, error_code = ?, error_summary = ?
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
            """,
            (
                state.value,
                now,
                terminal_at,
                result_json,
                error_code,
                error_summary,
                self._owner_key,
                self._workspace_key,
                lease.session_id,
                lease.job_id,
            ),
        )

    def _require_live_lease(
        self,
        connection: sqlite3.Connection,
        *,
        lease: SwarmWorkerLease,
        now: float,
    ) -> sqlite3.Row:
        if not isinstance(lease, SwarmWorkerLease):
            raise SwarmValidationError("A swarm worker lease is required.")
        session_id, job_id = _job_scope(lease.session_id, lease.job_id)
        row = self._job_row(connection, session_id=session_id, job_id=job_id)
        stored_hash = row["lease_token_sha256"]
        valid_token = isinstance(stored_hash, str) and hmac.compare_digest(
            stored_hash, _digest(lease.lease_token)
        )
        if (
            row["state"] != SwarmJobState.RUNNING.value
            or int(row["generation"]) != lease.generation
            or row["lease_expires_at"] is None
            or float(row["lease_expires_at"]) <= now
            or not valid_token
        ):
            raise SwarmLeaseLost("Swarm worker lease is no longer valid.")
        return row

    def _job_row(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        job_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM ide_swarm_jobs
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ? AND job_id = ?
            """,
            (self._owner_key, self._workspace_key, session_id, job_id),
        ).fetchone()
        if row is None:
            raise SwarmJobNotFound("Swarm job was not found.")
        return row

    def _now(self) -> float:
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise SwarmValidationError("Resumable swarm clock is invalid.") from exc
        if not math.isfinite(now) or now < 0:
            raise SwarmValidationError("Resumable swarm clock is invalid.")
        return now

    def _validate_storage_location(self) -> None:
        try:
            is_symlink = self.path.is_symlink()
        except OSError:
            raise SwarmValidationError("Resumable swarm storage path is invalid.") from None
        if is_symlink:
            raise SwarmValidationError("Resumable swarm database cannot be a symlink.")
        try:
            resolved = self.path.parent.resolve(strict=False) / self.path.name
            self.path = resolved
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return
        except OSError:
            raise SwarmValidationError("Resumable swarm storage path is invalid.") from None
        raise SwarmValidationError("Resumable swarm storage must be outside the workspace.")

    def _prepare_storage(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _restrict_permissions(self.path.parent, 0o700)
            with self._connection() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, SCHEMA_VERSION):
                    raise SwarmStorageError(
                        "Resumable swarm database schema is not supported by this version."
                    )
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS ide_swarm_jobs (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner_key TEXT NOT NULL,
                        workspace_key TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        idempotency_hash TEXT NOT NULL,
                        execution_spec_json TEXT NOT NULL,
                        execution_spec_sha256 TEXT NOT NULL,
                        permission_scope_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('queued', 'running', 'interrupted', 'succeeded',
                                'failed', 'cancelled')
                        ),
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        generation INTEGER NOT NULL CHECK (generation >= 0),
                        attempts INTEGER NOT NULL CHECK (attempts >= 0),
                        resume_count INTEGER NOT NULL CHECK (resume_count >= 0),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        terminal_at REAL,
                        lease_token_sha256 TEXT,
                        lease_worker_sha256 TEXT,
                        lease_expires_at REAL,
                        result_json TEXT,
                        error_code TEXT,
                        error_summary TEXT,
                        usage_calls INTEGER NOT NULL CHECK (usage_calls >= 0),
                        usage_input_tokens INTEGER NOT NULL CHECK (usage_input_tokens >= 0),
                        usage_output_tokens INTEGER NOT NULL CHECK (usage_output_tokens >= 0),
                        usage_cached_input_tokens INTEGER NOT NULL CHECK (
                            usage_cached_input_tokens >= 0
                        ),
                        usage_total_tokens INTEGER NOT NULL CHECK (usage_total_tokens >= 0),
                        UNIQUE (owner_key, workspace_key, session_id, job_id),
                        UNIQUE (owner_key, workspace_key, session_id, idempotency_hash),
                        CHECK (
                            (state = 'running' AND lease_token_sha256 IS NOT NULL
                                AND lease_worker_sha256 IS NOT NULL
                                AND lease_expires_at IS NOT NULL)
                            OR
                            (state != 'running' AND lease_token_sha256 IS NULL
                                AND lease_worker_sha256 IS NULL
                                AND lease_expires_at IS NULL)
                        ),
                        CHECK (
                            (state = 'succeeded' AND result_json IS NOT NULL
                                AND terminal_at IS NOT NULL)
                            OR
                            (state IN ('failed', 'cancelled') AND result_json IS NULL
                                AND terminal_at IS NOT NULL)
                            OR
                            (state IN ('queued', 'running', 'interrupted')
                                AND result_json IS NULL AND terminal_at IS NULL)
                        )
                    );
                    CREATE INDEX IF NOT EXISTS ide_swarm_jobs_session_order
                        ON ide_swarm_jobs(owner_key, workspace_key, session_id, sequence DESC);
                    CREATE INDEX IF NOT EXISTS ide_swarm_jobs_recovery
                        ON ide_swarm_jobs(owner_key, workspace_key, state, lease_expires_at);
                    CREATE TABLE IF NOT EXISTS ide_swarm_usage_events (
                        owner_key TEXT NOT NULL,
                        workspace_key TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        event_hash TEXT NOT NULL,
                        calls INTEGER NOT NULL CHECK (calls >= 0),
                        input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
                        output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
                        cached_input_tokens INTEGER NOT NULL CHECK (cached_input_tokens >= 0),
                        total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
                        recorded_at REAL NOT NULL,
                        PRIMARY KEY (
                            owner_key, workspace_key, session_id, job_id, event_hash
                        ),
                        FOREIGN KEY (owner_key, workspace_key, session_id, job_id)
                            REFERENCES ide_swarm_jobs(
                                owner_key, workspace_key, session_id, job_id
                            ) ON DELETE CASCADE
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            _restrict_permissions(self.path, 0o600)
        except ResumableSwarmError:
            raise
        except (OSError, sqlite3.Error):
            raise SwarmStorageError("Could not initialize resumable swarm storage.") from None

    @contextmanager
    def _connection(self) -> Any:
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
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise SwarmStorageError("Could not open resumable swarm storage.") from None
        try:
            yield connection
        except ResumableSwarmError:
            raise
        except sqlite3.Error:
            raise SwarmStorageError("Resumable swarm storage operation failed.") from None
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def _transaction(self) -> Any:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except ResumableSwarmError:
                _rollback_quietly(connection)
                raise
            except sqlite3.Error:
                _rollback_quietly(connection)
                raise SwarmStorageError("Resumable swarm storage operation failed.") from None
            except BaseException:
                _rollback_quietly(connection)
                raise


def _serialize_json_object(
    value: Mapping[str, Any],
    *,
    config: ResumableSwarmConfig,
    maximum_bytes: int,
    label: str,
    redact: bool,
) -> tuple[str, dict[str, JsonValue]]:
    if not isinstance(value, Mapping):
        raise SwarmValidationError(f"Swarm {label} must be a JSON object.")
    normalized = _normalize_json(value, config=config, redact=redact)
    if not isinstance(normalized, dict):
        raise SwarmValidationError(f"Swarm {label} must be a JSON object.")
    try:
        serialized = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise SwarmValidationError(f"Swarm {label} is not valid JSON.") from None
    if len(serialized.encode("utf-8")) > maximum_bytes:
        raise SwarmValidationError(f"Swarm {label} exceeds the size limit.")
    return serialized, normalized


def _normalize_json(
    value: Any,
    *,
    config: ResumableSwarmConfig,
    redact: bool,
) -> JsonValue:
    nodes = 0

    def visit(item: Any, depth: int, *, sensitive: bool = False) -> JsonValue:
        nonlocal nodes
        nodes += 1
        if nodes > config.max_json_nodes:
            raise SwarmValidationError("Swarm JSON has too many values.")
        if depth > config.max_json_depth:
            raise SwarmValidationError("Swarm JSON is nested too deeply.")
        if sensitive and redact:
            return _REDACTED
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int):
            if abs(item) > MAX_USAGE_VALUE:
                raise SwarmValidationError("Swarm JSON integer exceeds the safe range.")
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise SwarmValidationError("Swarm JSON contains an invalid number.")
            return item
        if isinstance(item, str):
            if len(item) > config.max_string_chars:
                raise SwarmValidationError("Swarm JSON string exceeds the size limit.")
            return _redact_text(item) if redact else item
        if isinstance(item, Mapping):
            result: dict[str, JsonValue] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise SwarmValidationError("Swarm JSON object keys must be strings.")
                if len(key) > 512 or _CONTROL_RE.search(key):
                    raise SwarmValidationError("Swarm JSON object key is invalid.")
                result[key] = visit(
                    child,
                    depth + 1,
                    sensitive=_is_sensitive_key(key),
                )
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1) for child in item]
        raise SwarmValidationError("Swarm JSON contains a non-JSON value.")

    return visit(value, 0)


def _permission_fingerprint(scope: Mapping[str, Any], config: ResumableSwarmConfig) -> str:
    serialized, _ = _serialize_json_object(
        scope,
        config=config,
        maximum_bytes=min(config.max_execution_spec_bytes, 256 * 1024),
        label="permission scope",
        redact=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _status_from_row(
    row: sqlite3.Row,
    *,
    maximum_error_chars: int,
) -> SwarmJobStatus:
    try:
        state = SwarmJobState(row["state"])
        status = SwarmJobStatus(
            job_id=_identifier(row["job_id"], "stored job", maximum=128),
            state=state,
            revision=_stored_nonnegative_int(row["revision"], minimum=1),
            attempts=_stored_nonnegative_int(row["attempts"]),
            resume_count=_stored_nonnegative_int(row["resume_count"]),
            created_at=_stored_time(row["created_at"]),
            updated_at=_stored_time(row["updated_at"]),
            started_at=_optional_stored_time(row["started_at"]),
            terminal_at=_optional_stored_time(row["terminal_at"]),
            lease_expires_at=_optional_stored_time(row["lease_expires_at"]),
            result_available=row["result_json"] is not None,
            error_code=(None if row["error_code"] is None else _error_code(row["error_code"])),
            error_summary=(
                None
                if row["error_summary"] is None
                else _public_text(row["error_summary"], maximum=maximum_error_chars)
            ),
            usage=_usage_from_row(row),
        )
    except (KeyError, TypeError, ValueError, OverflowError, SwarmValidationError):
        raise SwarmStorageError("Stored swarm job is invalid.") from None
    if status.state is SwarmJobState.RUNNING and status.lease_expires_at is None:
        raise SwarmStorageError("Stored swarm job is invalid.")
    if status.state is SwarmJobState.SUCCEEDED and not status.result_available:
        raise SwarmStorageError("Stored swarm job is invalid.")
    return status


def _usage_from_row(row: sqlite3.Row) -> SwarmUsage:
    return SwarmUsage(
        calls=_stored_nonnegative_int(row["usage_calls"]),
        input_tokens=_stored_nonnegative_int(row["usage_input_tokens"]),
        output_tokens=_stored_nonnegative_int(row["usage_output_tokens"]),
        cached_input_tokens=_stored_nonnegative_int(row["usage_cached_input_tokens"]),
        total_tokens=_stored_nonnegative_int(row["usage_total_tokens"]),
    )


def _validated_usage(usage: SwarmUsage) -> SwarmUsage:
    if not isinstance(usage, SwarmUsage):
        raise SwarmValidationError("Swarm usage must be a SwarmUsage value.")
    for value in (
        usage.calls,
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        usage.total_tokens,
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_USAGE_VALUE
        ):
            raise SwarmValidationError("Swarm usage values must be non-negative integers.")
    if usage.total_tokens < usage.input_tokens + usage.output_tokens:
        raise SwarmValidationError("Swarm total token usage is inconsistent.")
    return usage


def _safe_usage_sum(left: int, right: int) -> int:
    result = left + right
    if result > MAX_USAGE_VALUE:
        raise SwarmValidationError("Swarm usage total exceeds the safe range.")
    return result


def _require_permission(row: sqlite3.Row, current_hash: str) -> None:
    stored = row["permission_scope_sha256"]
    if not isinstance(stored, str) or not hmac.compare_digest(stored, current_hash):
        raise SwarmPermissionScopeChanged(
            "Swarm permission scope changed; start a new reviewed job."
        )


def _require_fresh_permission_grant(
    grant: Mapping[str, Any] | None,
    *,
    row: sqlite3.Row,
    session_id: str,
    job_id: str,
) -> None:
    if (
        not isinstance(grant, Mapping)
        or set(grant) != {"schema_version", "session_id", "job_id", "revision"}
        or grant.get("schema_version") != 1
        or grant.get("session_id") != session_id
        or grant.get("job_id") != job_id
        or isinstance(grant.get("revision"), bool)
        or not isinstance(grant.get("revision"), int)
        or int(grant["revision"]) != int(row["revision"])
    ):
        raise SwarmFreshPermissionGrantRequired(
            "A fresh, job-scoped permission grant is required to resume this swarm job."
        )


def _decode_stored_object(
    raw: Any,
    *,
    label: str,
    config: ResumableSwarmConfig,
    maximum_bytes: int,
) -> dict[str, JsonValue]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum_bytes:
        raise SwarmStorageError(f"Stored {label} is invalid.")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise SwarmStorageError(f"Stored {label} is invalid.") from None
    if not isinstance(decoded, dict):
        raise SwarmStorageError(f"Stored {label} is invalid.")
    try:
        _, normalized = _serialize_json_object(
            decoded,
            config=config,
            maximum_bytes=maximum_bytes,
            label=label,
            redact=True,
        )
    except SwarmValidationError:
        raise SwarmStorageError(f"Stored {label} is invalid.") from None
    return normalized


def _identifier(value: Any, kind: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise SwarmValidationError(f"Swarm {kind} identifier is invalid.")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or not _IDENTIFIER_RE.fullmatch(normalized)
        or ".." in normalized.split("/")
    ):
        raise SwarmValidationError(f"Swarm {kind} identifier is invalid.")
    return normalized


def _job_scope(session_id: Any, job_id: Any) -> tuple[str, str]:
    return (
        _identifier(session_id, "session", maximum=256),
        _identifier(job_id, "job", maximum=128),
    )


def _error_code(value: Any) -> str:
    if not isinstance(value, str):
        raise SwarmValidationError("Swarm error code is invalid.")
    normalized = value.strip()
    if len(normalized) > 80 or not _ERROR_CODE_RE.fullmatch(normalized):
        raise SwarmValidationError("Swarm error code is invalid.")
    return normalized


def _public_text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise SwarmValidationError("Swarm display text is invalid.")
    cleaned = " ".join(_CONTROL_RE.sub(" ", _redact_text(value)).split())
    if not cleaned:
        raise SwarmValidationError("Swarm display text is invalid.")
    return cleaned if len(cleaned) <= maximum else cleaned[: maximum - 3] + "..."


def _redact_text(value: str) -> str:
    return _SECRET_TOKEN_RE.sub(_REDACTED, _SECRET_ASSIGNMENT_RE.sub(_REDACTED, value))


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return (
        normalized in _SENSITIVE_KEY_PARTS
        or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)
        or normalized.startswith(_SENSITIVE_KEY_PREFIXES)
    )


def _duration(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not MIN_LEASE_SECONDS <= float(value) <= MAX_LEASE_SECONDS
    ):
        raise SwarmValidationError("Swarm lease duration is invalid.")
    return float(value)


def _deadline(now: float, duration: float) -> float:
    deadline = now + duration
    if not math.isfinite(deadline):
        raise SwarmValidationError("Swarm lease deadline is invalid.")
    return deadline


def _lease_token(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 32 or len(value) > 512:
        raise SwarmValidationError("Generated swarm lease token is invalid.")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_revision(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SwarmValidationError("Swarm revision is invalid.")
    return value


def _check_revision(row: sqlite3.Row, expected: int | None) -> None:
    if expected is not None and int(row["revision"]) != expected:
        raise SwarmRevisionConflict(int(row["revision"]))


def _limit(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise SwarmValidationError("Swarm list limit is invalid.")
    return value


def _stored_nonnegative_int(value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    if value < minimum or value > MAX_USAGE_VALUE:
        raise ValueError
    return value


def _stored_time(value: Any) -> float:
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError
    return normalized


def _optional_stored_time(value: Any) -> float | None:
    return None if value is None else _stored_time(value)


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError:
        raise SwarmStorageError("Could not secure resumable swarm storage.") from None


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass
