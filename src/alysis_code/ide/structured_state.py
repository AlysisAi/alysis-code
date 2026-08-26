"""Durable structured task and user-question state for IDE clients.

The store is deliberately independent from the agent/provider runtime.  It is
bound to one local owner and one canonical workspace, and every query includes
that scope.  Public records contain only bounded, redacted display text.

SQLite ``BEGIN IMMEDIATE`` transactions provide atomic cross-process writes.
Question resolution uses opaque, hashed lease tokens so a stale process cannot
answer after another process has recovered an expired claim.
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..branding import canonical_user_data_dir, env_get

SCHEMA_VERSION = 1
DEFAULT_QUESTION_LEASE_SECONDS = 120.0
MIN_QUESTION_LEASE_SECONDS = 1.0
MAX_QUESTION_LEASE_SECONDS = 60.0 * 60.0
MIN_QUESTION_TTL_SECONDS = 5.0
MAX_QUESTION_TTL_SECONDS = 7.0 * 24.0 * 60.0 * 60.0

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SECRET_TOKEN_RE = re.compile(
    r"(?i)(?:"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"gh[opusr]_[A-Za-z0-9]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r")"
)
_REDACTED = "[REDACTED]"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class QuestionSetStatus(str, Enum):
    PENDING = "pending"
    RESOLVING = "resolving"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_QUESTION_STATUSES = frozenset(
    {
        QuestionSetStatus.ANSWERED,
        QuestionSetStatus.CANCELLED,
        QuestionSetStatus.EXPIRED,
    }
)


class StructuredStateError(RuntimeError):
    """Base error whose message is safe to surface to an IDE client."""


class StructuredStateValidationError(StructuredStateError, ValueError):
    pass


class StructuredStateStorageError(StructuredStateError):
    pass


class _StructuredStateTransientStorageError(StructuredStateStorageError):
    """Internal marker for SQLite busy/locked errors that are safe to retry."""


class TaskRevisionConflict(StructuredStateError):
    def __init__(self, current_revision: int) -> None:
        super().__init__("Task ledger changed; reload it before updating.")
        self.current_revision = current_revision


class QuestionIdempotencyConflict(StructuredStateError):
    pass


class QuestionNotFound(StructuredStateError):
    pass


class QuestionStateError(StructuredStateError):
    pass


class QuestionLeaseLost(StructuredStateError):
    pass


class StructuredStateCapacityError(StructuredStateError):
    pass


@dataclass(frozen=True, slots=True)
class StructuredStateConfig:
    max_tasks_per_session: int = 100
    max_task_title_chars: int = 240
    max_question_sets_per_session: int = 50
    max_question_prompt_chars: int = 600
    max_option_label_chars: int = 100
    max_option_description_chars: int = 300
    max_raw_text_chars: int = 4096
    max_list_limit: int = 200
    busy_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        integers = (
            self.max_tasks_per_session,
            self.max_task_title_chars,
            self.max_question_sets_per_session,
            self.max_question_prompt_chars,
            self.max_option_label_chars,
            self.max_option_description_chars,
            self.max_raw_text_chars,
            self.max_list_limit,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise StructuredStateValidationError(
                "Structured-state limits must be positive integers."
            )
        if (
            self.max_tasks_per_session > 1_000
            or self.max_question_sets_per_session > 1_000
            or self.max_list_limit > 1_000
            or self.max_raw_text_chars > 65_536
            or self.max_task_title_chars > self.max_raw_text_chars
            or self.max_question_prompt_chars > self.max_raw_text_chars
            or self.max_option_label_chars > self.max_raw_text_chars
            or self.max_option_description_chars > self.max_raw_text_chars
        ):
            raise StructuredStateValidationError("Structured-state limits exceed safe bounds.")
        if (
            isinstance(self.busy_timeout_seconds, bool)
            or not isinstance(self.busy_timeout_seconds, (int, float))
            or not math.isfinite(self.busy_timeout_seconds)
            or not 0 < self.busy_timeout_seconds <= 60
        ):
            raise StructuredStateValidationError("Structured-state busy timeout is invalid.")


@dataclass(frozen=True, slots=True)
class TaskItem:
    task_id: str
    title: str
    status: TaskStatus

    def public_payload(self) -> dict[str, str]:
        return {"task_id": self.task_id, "title": self.title, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class TaskLedger:
    revision: int
    tasks: tuple[TaskItem, ...]
    updated_at: float | None

    def public_payload(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "tasks": [task.public_payload() for task in self.tasks],
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class QuestionOption:
    option_id: str
    label: str
    description: str

    def public_payload(self) -> dict[str, str]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class StructuredQuestion:
    question_id: str
    prompt: str
    options: tuple[QuestionOption, ...]

    def public_payload(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "prompt": self.prompt,
            "options": [option.public_payload() for option in self.options],
        }


@dataclass(frozen=True, slots=True)
class QuestionAnswer:
    question_id: str
    option_id: str

    def public_payload(self) -> dict[str, str]:
        return {"question_id": self.question_id, "option_id": self.option_id}


@dataclass(frozen=True, slots=True)
class QuestionSet:
    question_set_id: str
    status: QuestionSetStatus
    revision: int
    questions: tuple[StructuredQuestion, ...]
    answers: tuple[QuestionAnswer, ...]
    created_at: float
    updated_at: float
    expires_at: float
    terminal_at: float | None
    resolution_attempts: int

    def public_payload(self) -> dict[str, Any]:
        return {
            "question_set_id": self.question_set_id,
            "status": self.status.value,
            "revision": self.revision,
            "questions": [question.public_payload() for question in self.questions],
            "answers": [answer.public_payload() for answer in self.answers],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "terminal_at": self.terminal_at,
            "resolution_attempts": self.resolution_attempts,
        }


@dataclass(frozen=True, slots=True)
class QuestionCreationResult:
    question_set: QuestionSet
    created: bool


@dataclass(frozen=True, slots=True)
class QuestionResolutionLease:
    question_set: QuestionSet
    lease_token: str = field(repr=False)
    expires_at: float


def default_structured_state_path() -> Path:
    """Return a user-private state path, outside any project by default."""

    override = str(env_get("ALYSIS_DATA_DIR") or "").strip()
    data_dir = Path(override).expanduser() if override else canonical_user_data_dir()
    return data_dir / "ide" / "structured-state.sqlite3"


class DurableStructuredState:
    """Workspace/owner-scoped durable task and question registry."""

    def __init__(
        self,
        *,
        owner_id: str,
        workspace_root: str | os.PathLike[str],
        path: str | os.PathLike[str] | None = None,
        config: StructuredStateConfig | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        lease_factory: Callable[[], str] | None = None,
    ) -> None:
        owner_id = _validate_identifier(owner_id, "owner", max_length=256)
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise StructuredStateValidationError(
                "Structured-state workspace path is invalid."
            ) from None
        if not workspace.is_dir():
            raise StructuredStateValidationError("Structured-state workspace must be a directory.")
        self.workspace_root = workspace
        try:
            self.path = (
                Path(path).expanduser() if path is not None else default_structured_state_path()
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            raise StructuredStateValidationError(
                "Structured-state storage path is invalid."
            ) from None
        self.config = config or StructuredStateConfig()
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lease_factory = lease_factory or (lambda: f"lease_{secrets.token_urlsafe(32)}")
        self._owner_key = _scope_digest(owner_id)
        self._workspace_key = _scope_digest(os.path.normcase(os.fspath(workspace)))
        self._lock = threading.RLock()
        self._validate_storage_location()
        self._prepare_storage()

    def get_task_ledger(self, *, session_id: str) -> TaskLedger:
        session_id = _validate_identifier(session_id, "session", max_length=256)
        with self._connection() as connection:
            return self._task_ledger_locked(connection, session_id)

    def replace_tasks(
        self,
        *,
        session_id: str,
        expected_revision: int,
        tasks: Sequence[TaskItem | Mapping[str, Any]],
    ) -> TaskLedger:
        """Atomically replace a session ledger using revision compare-and-swap."""

        session_id = _validate_identifier(session_id, "session", max_length=256)
        expected_revision = _validate_revision(expected_revision)
        normalized = self._normalize_tasks(tasks)
        now = self._now()
        with self._lock, self._transaction() as connection:
            current = self._task_ledger_locked(connection, session_id)
            if current.revision != expected_revision:
                raise TaskRevisionConflict(current.revision)
            next_revision = current.revision + 1
            connection.execute(
                """
                INSERT INTO ide_task_ledgers (
                    owner_key, workspace_key, session_id, revision, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_key, workspace_key, session_id)
                DO UPDATE SET revision = excluded.revision, updated_at = excluded.updated_at
                """,
                (self._owner_key, self._workspace_key, session_id, next_revision, now),
            )
            connection.execute(
                """
                DELETE FROM ide_tasks
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                """,
                (self._owner_key, self._workspace_key, session_id),
            )
            connection.executemany(
                """
                INSERT INTO ide_tasks (
                    owner_key, workspace_key, session_id, position, task_id, title, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self._owner_key,
                        self._workspace_key,
                        session_id,
                        position,
                        task.task_id,
                        task.title,
                        task.status.value,
                    )
                    for position, task in enumerate(normalized)
                ],
            )
            return TaskLedger(revision=next_revision, tasks=normalized, updated_at=now)

    def create_question_set(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        questions: Sequence[StructuredQuestion | Mapping[str, Any]],
        expires_in_seconds: float,
    ) -> QuestionCreationResult:
        session_id = _validate_identifier(session_id, "session", max_length=256)
        idempotency_key = _validate_identifier(idempotency_key, "idempotency", max_length=256)
        ttl = _validate_duration(
            expires_in_seconds,
            minimum=MIN_QUESTION_TTL_SECONDS,
            maximum=MAX_QUESTION_TTL_SECONDS,
            kind="question expiry",
        )
        normalized = self._normalize_questions(questions)
        questions_json = _questions_json(normalized)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"questions": json.loads(questions_json), "ttl": ttl},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        idempotency_hash = _scope_digest(idempotency_key)
        now = self._now()
        expires_at = _safe_deadline(now, ttl)
        with self._lock, self._transaction() as connection:
            self._refresh_questions_locked(connection, session_id=session_id, now=now)
            existing = connection.execute(
                """
                SELECT * FROM ide_question_sets
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                    AND idempotency_hash = ?
                """,
                (self._owner_key, self._workspace_key, session_id, idempotency_hash),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["payload_sha256"]), fingerprint):
                    raise QuestionIdempotencyConflict(
                        "Idempotency key was already used for different questions."
                    )
                return QuestionCreationResult(
                    question_set=_question_set_from_row(existing, config=self.config), created=False
                )

            outstanding = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM ide_question_sets
                    WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                        AND status IN ('pending', 'resolving')
                    """,
                    (self._owner_key, self._workspace_key, session_id),
                ).fetchone()[0]
            )
            if outstanding >= self.config.max_question_sets_per_session:
                raise StructuredStateCapacityError("Session has too many open question sets.")
            question_set_id = _validate_identifier(
                self._id_factory(), "generated question set", max_length=256
            )
            try:
                connection.execute(
                    """
                    INSERT INTO ide_question_sets (
                        owner_key, workspace_key, session_id, question_set_id,
                        idempotency_hash, payload_json, payload_sha256, ttl_seconds,
                        status, revision, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)
                    """,
                    (
                        self._owner_key,
                        self._workspace_key,
                        session_id,
                        question_set_id,
                        idempotency_hash,
                        questions_json,
                        fingerprint,
                        ttl,
                        now,
                        now,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT * FROM ide_question_sets
                    WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                        AND idempotency_hash = ?
                    """,
                    (self._owner_key, self._workspace_key, session_id, idempotency_hash),
                ).fetchone()
                if existing is None:
                    raise StructuredStateStorageError("Could not persist question set.") from None
                if not hmac.compare_digest(str(existing["payload_sha256"]), fingerprint):
                    raise QuestionIdempotencyConflict(
                        "Idempotency key was already used for different questions."
                    ) from None
                return QuestionCreationResult(
                    question_set=_question_set_from_row(existing, config=self.config), created=False
                )
            row = self._question_row_locked(connection, session_id, question_set_id)
            return QuestionCreationResult(
                question_set=_question_set_from_row(row, config=self.config), created=True
            )

    def get_question_set(self, *, session_id: str, question_set_id: str) -> QuestionSet | None:
        session_id, question_set_id = _validate_question_identifiers(session_id, question_set_id)
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._refresh_questions_locked(connection, session_id=session_id, now=now)
            row = self._optional_question_row_locked(connection, session_id, question_set_id)
            return None if row is None else _question_set_from_row(row, config=self.config)

    def list_question_sets(
        self,
        *,
        session_id: str,
        statuses: Iterable[QuestionSetStatus | str] | None = None,
        limit: int = 50,
    ) -> list[QuestionSet]:
        session_id = _validate_identifier(session_id, "session", max_length=256)
        status_values = _normalize_question_statuses(statuses)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.config.max_list_limit
        ):
            raise StructuredStateValidationError("Question-set list limit is invalid.")
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._refresh_questions_locked(connection, session_id=session_id, now=now)
            params: list[Any] = [self._owner_key, self._workspace_key, session_id]
            where = ""
            if status_values:
                placeholders = ",".join("?" for _ in status_values)
                where = f" AND status IN ({placeholders})"
                params.extend(status_values)
            params.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM ide_question_sets
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?{where}
                ORDER BY created_at DESC, question_set_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [_question_set_from_row(row, config=self.config) for row in rows]

    def claim_question_set(
        self,
        *,
        session_id: str,
        question_set_id: str,
        resolver_id: str,
        lease_seconds: float = DEFAULT_QUESTION_LEASE_SECONDS,
    ) -> QuestionResolutionLease | None:
        session_id, question_set_id = _validate_question_identifiers(session_id, question_set_id)
        resolver_id = _validate_identifier(resolver_id, "resolver", max_length=256)
        lease_seconds = _validate_duration(
            lease_seconds,
            minimum=MIN_QUESTION_LEASE_SECONDS,
            maximum=MAX_QUESTION_LEASE_SECONDS,
            kind="question lease",
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._refresh_questions_locked(connection, session_id=session_id, now=now)
            row = self._question_row_locked(connection, session_id, question_set_id)
            status = QuestionSetStatus(row["status"])
            if status in TERMINAL_QUESTION_STATUSES:
                raise QuestionStateError("Question set is already terminal.")
            if status is QuestionSetStatus.RESOLVING:
                return None
            token = _validate_identifier(
                self._lease_factory(), "generated question lease", max_length=256
            )
            expires_at = min(_safe_deadline(now, lease_seconds), float(row["expires_at"]))
            connection.execute(
                """
                UPDATE ide_question_sets
                SET status = 'resolving', revision = revision + 1,
                    updated_at = ?, resolution_attempts = resolution_attempts + 1,
                    resolution_token_hash = ?, resolution_owner_hash = ?,
                    resolution_expires_at = ?
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                    AND question_set_id = ? AND status = 'pending'
                """,
                (
                    now,
                    _scope_digest(token),
                    _scope_digest(resolver_id),
                    expires_at,
                    self._owner_key,
                    self._workspace_key,
                    session_id,
                    question_set_id,
                ),
            )
            claimed = self._question_row_locked(connection, session_id, question_set_id)
            if claimed["status"] != QuestionSetStatus.RESOLVING.value:
                return None
            return QuestionResolutionLease(
                question_set=_question_set_from_row(claimed, config=self.config),
                lease_token=token,
                expires_at=expires_at,
            )

    def renew_question_lease(
        self,
        *,
        session_id: str,
        question_set_id: str,
        lease_token: str,
        lease_seconds: float = DEFAULT_QUESTION_LEASE_SECONDS,
    ) -> QuestionResolutionLease:
        session_id, question_set_id, lease_token = _validate_question_lease_identifiers(
            session_id, question_set_id, lease_token
        )
        lease_seconds = _validate_duration(
            lease_seconds,
            minimum=MIN_QUESTION_LEASE_SECONDS,
            maximum=MAX_QUESTION_LEASE_SECONDS,
            kind="question lease",
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._require_live_question_lease(
                connection,
                session_id=session_id,
                question_set_id=question_set_id,
                lease_token=lease_token,
                now=now,
            )
            expires_at = min(_safe_deadline(now, lease_seconds), float(row["expires_at"]))
            connection.execute(
                """
                UPDATE ide_question_sets
                SET revision = revision + 1, updated_at = ?, resolution_expires_at = ?
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                    AND question_set_id = ?
                """,
                (
                    now,
                    expires_at,
                    self._owner_key,
                    self._workspace_key,
                    session_id,
                    question_set_id,
                ),
            )
            renewed = self._question_row_locked(connection, session_id, question_set_id)
            return QuestionResolutionLease(
                question_set=_question_set_from_row(renewed, config=self.config),
                lease_token=lease_token,
                expires_at=expires_at,
            )

    def release_question_set(
        self, *, session_id: str, question_set_id: str, lease_token: str
    ) -> QuestionSet:
        session_id, question_set_id, lease_token = _validate_question_lease_identifiers(
            session_id, question_set_id, lease_token
        )
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._require_live_question_lease(
                connection,
                session_id=session_id,
                question_set_id=question_set_id,
                lease_token=lease_token,
                now=now,
            )
            self._set_question_pending_locked(connection, session_id, question_set_id, now)
            return _question_set_from_row(
                self._question_row_locked(connection, session_id, question_set_id),
                config=self.config,
            )

    def answer_question_set(
        self,
        *,
        session_id: str,
        question_set_id: str,
        lease_token: str,
        answers: Mapping[str, str],
    ) -> QuestionSet:
        session_id, question_set_id, lease_token = _validate_question_lease_identifiers(
            session_id, question_set_id, lease_token
        )
        if not isinstance(answers, Mapping):
            raise StructuredStateValidationError("Question answers must be an object.")
        now = self._now()
        with self._lock, self._transaction() as connection:
            row = self._require_live_question_lease(
                connection,
                session_id=session_id,
                question_set_id=question_set_id,
                lease_token=lease_token,
                now=now,
            )
            questions = _questions_from_json(row["payload_json"], config=self.config)
            normalized_answers = _normalize_answers(answers, questions)
            answers_json = json.dumps(
                {answer.question_id: answer.option_id for answer in normalized_answers},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            connection.execute(
                """
                UPDATE ide_question_sets
                SET status = 'answered', revision = revision + 1, updated_at = ?,
                    answers_json = ?, terminal_at = ?, resolution_token_hash = NULL,
                    resolution_owner_hash = NULL, resolution_expires_at = NULL
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                    AND question_set_id = ?
                """,
                (
                    now,
                    answers_json,
                    now,
                    self._owner_key,
                    self._workspace_key,
                    session_id,
                    question_set_id,
                ),
            )
            return _question_set_from_row(
                self._question_row_locked(connection, session_id, question_set_id),
                config=self.config,
            )

    def cancel_question_set(
        self,
        *,
        session_id: str,
        question_set_id: str,
        lease_token: str | None = None,
    ) -> QuestionSet:
        session_id, question_set_id = _validate_question_identifiers(session_id, question_set_id)
        if lease_token is not None:
            lease_token = _validate_identifier(lease_token, "question lease", max_length=256)
        now = self._now()
        with self._lock, self._transaction() as connection:
            self._refresh_questions_locked(connection, session_id=session_id, now=now)
            row = self._question_row_locked(connection, session_id, question_set_id)
            status = QuestionSetStatus(row["status"])
            if status in TERMINAL_QUESTION_STATUSES:
                raise QuestionStateError("Question set is already terminal.")
            if status is QuestionSetStatus.RESOLVING:
                if lease_token is None or not _token_matches(
                    row["resolution_token_hash"], lease_token
                ):
                    raise QuestionLeaseLost("Question resolution lease is no longer valid.")
                if (
                    row["resolution_expires_at"] is None
                    or float(row["resolution_expires_at"]) <= now
                ):
                    raise QuestionLeaseLost("Question resolution lease is no longer valid.")
            elif lease_token is not None:
                # A token is evidence that the caller is attempting a fenced
                # resolver operation.  Once released or expired it must never
                # gain the authority of an ordinary unclaimed UI cancellation.
                raise QuestionLeaseLost("Question resolution lease is no longer valid.")
            connection.execute(
                """
                UPDATE ide_question_sets
                SET status = 'cancelled', revision = revision + 1, updated_at = ?,
                    terminal_at = ?, resolution_token_hash = NULL,
                    resolution_owner_hash = NULL, resolution_expires_at = NULL
                WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                    AND question_set_id = ?
                """,
                (
                    now,
                    now,
                    self._owner_key,
                    self._workspace_key,
                    session_id,
                    question_set_id,
                ),
            )
            return _question_set_from_row(
                self._question_row_locked(connection, session_id, question_set_id),
                config=self.config,
            )

    def _normalize_tasks(
        self, tasks: Sequence[TaskItem | Mapping[str, Any]]
    ) -> tuple[TaskItem, ...]:
        if isinstance(tasks, (str, bytes)) or not isinstance(tasks, Sequence):
            raise StructuredStateValidationError("Tasks must be an array.")
        if len(tasks) > self.config.max_tasks_per_session:
            raise StructuredStateCapacityError("Task ledger exceeds its task limit.")
        normalized: list[TaskItem] = []
        task_ids: set[str] = set()
        in_progress = 0
        for raw in tasks:
            if isinstance(raw, TaskItem):
                task_id, title, status_raw = raw.task_id, raw.title, raw.status
            elif isinstance(raw, Mapping):
                extra = set(raw) - {"task_id", "title", "status"}
                if extra:
                    raise StructuredStateValidationError("Task contains unsupported fields.")
                task_id, title, status_raw = raw.get("task_id"), raw.get("title"), raw.get("status")
            else:
                raise StructuredStateValidationError("Task entry is invalid.")
            task_id = _validate_identifier(task_id, "task", max_length=128)
            if task_id in task_ids:
                raise StructuredStateValidationError("Task identifiers must be unique.")
            task_ids.add(task_id)
            title = _public_text(
                title,
                kind="task title",
                maximum=self.config.max_task_title_chars,
                raw_maximum=self.config.max_raw_text_chars,
            )
            try:
                status = TaskStatus(status_raw)
            except (TypeError, ValueError):
                raise StructuredStateValidationError("Task status is invalid.") from None
            in_progress += status is TaskStatus.IN_PROGRESS
            normalized.append(TaskItem(task_id=task_id, title=title, status=status))
        if in_progress > 1:
            raise StructuredStateValidationError("Only one task may be in progress.")
        return tuple(normalized)

    def _normalize_questions(
        self, questions: Sequence[StructuredQuestion | Mapping[str, Any]]
    ) -> tuple[StructuredQuestion, ...]:
        if isinstance(questions, (str, bytes)) or not isinstance(questions, Sequence):
            raise StructuredStateValidationError("Questions must be an array.")
        if not 1 <= len(questions) <= 3:
            raise StructuredStateValidationError(
                "A question set must contain one to three questions."
            )
        result: list[StructuredQuestion] = []
        question_ids: set[str] = set()
        for raw_question in questions:
            if isinstance(raw_question, StructuredQuestion):
                question_id = raw_question.question_id
                prompt = raw_question.prompt
                raw_options: Any = raw_question.options
            elif isinstance(raw_question, Mapping):
                extra = set(raw_question) - {"question_id", "prompt", "options"}
                if extra:
                    raise StructuredStateValidationError("Question contains unsupported fields.")
                question_id = raw_question.get("question_id")
                prompt = raw_question.get("prompt")
                raw_options = raw_question.get("options")
            else:
                raise StructuredStateValidationError("Question entry is invalid.")
            question_id = _validate_identifier(question_id, "question", max_length=128)
            if question_id in question_ids:
                raise StructuredStateValidationError("Question identifiers must be unique.")
            question_ids.add(question_id)
            prompt = _public_text(
                prompt,
                kind="question prompt",
                maximum=self.config.max_question_prompt_chars,
                raw_maximum=self.config.max_raw_text_chars,
            )
            if isinstance(raw_options, (str, bytes)) or not isinstance(raw_options, Sequence):
                raise StructuredStateValidationError("Question options must be an array.")
            if not 2 <= len(raw_options) <= 3:
                raise StructuredStateValidationError(
                    "Each question must have two or three options."
                )
            options: list[QuestionOption] = []
            option_ids: set[str] = set()
            option_labels: set[str] = set()
            for raw_option in raw_options:
                if isinstance(raw_option, QuestionOption):
                    option_id = raw_option.option_id
                    label = raw_option.label
                    description = raw_option.description
                elif isinstance(raw_option, Mapping):
                    extra = set(raw_option) - {"option_id", "label", "description"}
                    if extra:
                        raise StructuredStateValidationError(
                            "Question option contains unsupported fields."
                        )
                    option_id = raw_option.get("option_id")
                    label = raw_option.get("label")
                    description = raw_option.get("description")
                else:
                    raise StructuredStateValidationError("Question option is invalid.")
                option_id = _validate_identifier(option_id, "option", max_length=128)
                if option_id in option_ids:
                    raise StructuredStateValidationError(
                        "Option identifiers must be unique per question."
                    )
                option_ids.add(option_id)
                label = _public_text(
                    label,
                    kind="option label",
                    maximum=self.config.max_option_label_chars,
                    raw_maximum=self.config.max_raw_text_chars,
                )
                label_key = label.casefold()
                if label_key in option_labels:
                    raise StructuredStateValidationError(
                        "Option labels must be distinct per question."
                    )
                option_labels.add(label_key)
                description = _public_text(
                    description,
                    kind="option description",
                    maximum=self.config.max_option_description_chars,
                    raw_maximum=self.config.max_raw_text_chars,
                )
                options.append(
                    QuestionOption(option_id=option_id, label=label, description=description)
                )
            result.append(
                StructuredQuestion(question_id=question_id, prompt=prompt, options=tuple(options))
            )
        return tuple(result)

    def _task_ledger_locked(self, connection: sqlite3.Connection, session_id: str) -> TaskLedger:
        ledger = connection.execute(
            """
            SELECT revision, updated_at FROM ide_task_ledgers
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
            """,
            (self._owner_key, self._workspace_key, session_id),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT position, task_id, title, status FROM ide_tasks
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
            ORDER BY position ASC
            """,
            (self._owner_key, self._workspace_key, session_id),
        ).fetchall()
        try:
            invalid_positions = any(
                int(row["position"]) != position for position, row in enumerate(rows)
            )
        except (TypeError, ValueError):
            raise StructuredStateStorageError("Stored task ledger is invalid.") from None
        if len(rows) > self.config.max_tasks_per_session or invalid_positions:
            raise StructuredStateStorageError("Stored task ledger is invalid.")
        try:
            tasks = tuple(
                TaskItem(
                    task_id=_stored_identifier(row["task_id"], "task", max_length=128),
                    title=_stored_public_text(
                        row["title"],
                        kind="task title",
                        maximum=self.config.max_task_title_chars,
                        raw_maximum=self.config.max_raw_text_chars,
                    ),
                    status=TaskStatus(row["status"]),
                )
                for row in rows
            )
        except (TypeError, ValueError):
            raise StructuredStateStorageError("Stored task ledger is invalid.") from None
        if ledger is None:
            if tasks:
                raise StructuredStateStorageError("Stored task ledger is inconsistent.")
            return TaskLedger(revision=0, tasks=(), updated_at=None)
        try:
            revision = int(ledger["revision"])
            updated_at = _stored_nonnegative_number(ledger["updated_at"])
            if revision < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise StructuredStateStorageError("Stored task ledger is invalid.") from None
        return TaskLedger(revision=revision, tasks=tasks, updated_at=updated_at)

    def _refresh_questions_locked(
        self, connection: sqlite3.Connection, *, session_id: str, now: float
    ) -> None:
        scope = (self._owner_key, self._workspace_key, session_id)
        connection.execute(
            """
            UPDATE ide_question_sets
            SET status = 'expired', revision = revision + 1, updated_at = ?, terminal_at = ?,
                resolution_token_hash = NULL, resolution_owner_hash = NULL,
                resolution_expires_at = NULL
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                AND status IN ('pending', 'resolving') AND expires_at <= ?
            """,
            (now, now, *scope, now),
        )
        connection.execute(
            """
            UPDATE ide_question_sets
            SET status = 'pending', revision = revision + 1, updated_at = ?,
                resolution_token_hash = NULL, resolution_owner_hash = NULL,
                resolution_expires_at = NULL
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                AND status = 'resolving' AND resolution_expires_at <= ? AND expires_at > ?
            """,
            (now, *scope, now, now),
        )

    def _set_question_pending_locked(
        self, connection: sqlite3.Connection, session_id: str, question_set_id: str, now: float
    ) -> None:
        connection.execute(
            """
            UPDATE ide_question_sets
            SET status = 'pending', revision = revision + 1, updated_at = ?,
                resolution_token_hash = NULL, resolution_owner_hash = NULL,
                resolution_expires_at = NULL
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                AND question_set_id = ?
            """,
            (
                now,
                self._owner_key,
                self._workspace_key,
                session_id,
                question_set_id,
            ),
        )

    def _require_live_question_lease(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        question_set_id: str,
        lease_token: str,
        now: float,
    ) -> sqlite3.Row:
        self._refresh_questions_locked(connection, session_id=session_id, now=now)
        row = self._question_row_locked(connection, session_id, question_set_id)
        if (
            row["status"] != QuestionSetStatus.RESOLVING.value
            or row["resolution_expires_at"] is None
            or float(row["resolution_expires_at"]) <= now
            or not _token_matches(row["resolution_token_hash"], lease_token)
        ):
            raise QuestionLeaseLost("Question resolution lease is no longer valid.")
        return row

    def _optional_question_row_locked(
        self, connection: sqlite3.Connection, session_id: str, question_set_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM ide_question_sets
            WHERE owner_key = ? AND workspace_key = ? AND session_id = ?
                AND question_set_id = ?
            """,
            (self._owner_key, self._workspace_key, session_id, question_set_id),
        ).fetchone()

    def _question_row_locked(
        self, connection: sqlite3.Connection, session_id: str, question_set_id: str
    ) -> sqlite3.Row:
        row = self._optional_question_row_locked(connection, session_id, question_set_id)
        if row is None:
            raise QuestionNotFound("Question set was not found.")
        return row

    def _now(self) -> float:
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise StructuredStateValidationError("Structured-state clock is invalid.") from exc
        if not math.isfinite(now) or now < 0:
            raise StructuredStateValidationError("Structured-state clock is invalid.")
        return now

    def _validate_storage_location(self) -> None:
        try:
            is_symlink = self.path.is_symlink()
        except OSError:
            raise StructuredStateValidationError(
                "Structured-state storage path is invalid."
            ) from None
        if is_symlink:
            raise StructuredStateValidationError("Structured-state database cannot be a symlink.")
        try:
            resolved = self.path.parent.resolve(strict=False) / self.path.name
            self.path = resolved
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return
        except OSError:
            raise StructuredStateValidationError(
                "Structured-state storage path is invalid."
            ) from None
        raise StructuredStateValidationError(
            "Structured-state storage must be outside the workspace."
        )

    def _prepare_storage(self) -> None:
        deadline = time.monotonic() + self.config.busy_timeout_seconds
        while True:
            try:
                self._prepare_storage_once()
                return
            except _StructuredStateTransientStorageError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StructuredStateStorageError(
                        "Could not initialize structured-state storage."
                    ) from None
                time.sleep(min(0.05, remaining))

    def _prepare_storage_once(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _restrict_permissions(self.path.parent, 0o700)
            with self._connection() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, SCHEMA_VERSION):
                    raise StructuredStateStorageError(
                        "Structured-state database schema is not supported by this version."
                    )
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS ide_task_ledgers (
                        owner_key TEXT NOT NULL,
                        workspace_key TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision >= 0),
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (owner_key, workspace_key, session_id)
                    );
                    CREATE TABLE IF NOT EXISTS ide_tasks (
                        owner_key TEXT NOT NULL,
                        workspace_key TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        position INTEGER NOT NULL CHECK (position >= 0),
                        task_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'in_progress', 'completed', 'blocked')
                        ),
                        PRIMARY KEY (owner_key, workspace_key, session_id, task_id),
                        UNIQUE (owner_key, workspace_key, session_id, position),
                        FOREIGN KEY (owner_key, workspace_key, session_id)
                            REFERENCES ide_task_ledgers(owner_key, workspace_key, session_id)
                            ON DELETE CASCADE
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS ide_tasks_one_in_progress
                        ON ide_tasks(owner_key, workspace_key, session_id)
                        WHERE status = 'in_progress';
                    CREATE TABLE IF NOT EXISTS ide_question_sets (
                        owner_key TEXT NOT NULL,
                        workspace_key TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        question_set_id TEXT NOT NULL,
                        idempotency_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        ttl_seconds REAL NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'resolving', 'answered', 'cancelled', 'expired')
                        ),
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        terminal_at REAL,
                        resolution_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
                            resolution_attempts >= 0
                        ),
                        resolution_token_hash TEXT,
                        resolution_owner_hash TEXT,
                        resolution_expires_at REAL,
                        answers_json TEXT,
                        PRIMARY KEY (owner_key, workspace_key, session_id, question_set_id),
                        UNIQUE (owner_key, workspace_key, session_id, idempotency_hash),
                        CHECK (
                            (status = 'resolving' AND resolution_token_hash IS NOT NULL
                                AND resolution_owner_hash IS NOT NULL
                                AND resolution_expires_at IS NOT NULL)
                            OR
                            (status != 'resolving' AND resolution_token_hash IS NULL
                                AND resolution_owner_hash IS NULL
                                AND resolution_expires_at IS NULL)
                        ),
                        CHECK (
                            (status = 'answered' AND answers_json IS NOT NULL
                                AND terminal_at IS NOT NULL)
                            OR
                            (status IN ('cancelled', 'expired') AND answers_json IS NULL
                                AND terminal_at IS NOT NULL)
                            OR
                            (status IN ('pending', 'resolving') AND answers_json IS NULL
                                AND terminal_at IS NULL)
                        )
                    );
                    CREATE INDEX IF NOT EXISTS ide_question_sets_session_order
                        ON ide_question_sets(
                            owner_key, workspace_key, session_id, created_at DESC
                        );
                    CREATE INDEX IF NOT EXISTS ide_question_sets_recovery
                        ON ide_question_sets(status, expires_at, resolution_expires_at);
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            _restrict_permissions(self.path, 0o600)
        except StructuredStateError:
            raise
        except (OSError, sqlite3.Error):
            raise StructuredStateStorageError(
                "Could not initialize structured-state storage."
            ) from None

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
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            if _is_transient_sqlite_error(exc):
                raise _StructuredStateTransientStorageError(
                    "Structured-state storage is temporarily busy."
                ) from None
            raise StructuredStateStorageError("Could not open structured-state storage.") from None
        try:
            yield connection
        except StructuredStateError:
            raise
        except sqlite3.Error as exc:
            if _is_transient_sqlite_error(exc):
                raise _StructuredStateTransientStorageError(
                    "Structured-state storage is temporarily busy."
                ) from None
            raise StructuredStateStorageError("Structured-state operation failed.") from None
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
            except StructuredStateError:
                _rollback_quietly(connection)
                raise
            except sqlite3.Error:
                _rollback_quietly(connection)
                raise StructuredStateStorageError("Structured-state operation failed.") from None
            except BaseException:
                _rollback_quietly(connection)
                raise


def _questions_json(questions: Sequence[StructuredQuestion]) -> str:
    return json.dumps(
        [question.public_payload() for question in questions],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _questions_from_json(
    raw: Any, *, config: StructuredStateConfig
) -> tuple[StructuredQuestion, ...]:
    try:
        if not isinstance(raw, str):
            raise ValueError
        decoded = json.loads(raw)
        if not isinstance(decoded, list) or not 1 <= len(decoded) <= 3:
            raise ValueError
        questions: list[StructuredQuestion] = []
        question_ids: set[str] = set()
        for entry in decoded:
            if not isinstance(entry, dict) or set(entry) != {"question_id", "prompt", "options"}:
                raise ValueError
            question_id = _stored_identifier(entry["question_id"], "question", max_length=128)
            if question_id in question_ids:
                raise ValueError
            question_ids.add(question_id)
            prompt = _stored_public_text(
                entry["prompt"],
                kind="question prompt",
                maximum=config.max_question_prompt_chars,
                raw_maximum=config.max_raw_text_chars,
            )
            options_raw = entry["options"]
            if not isinstance(options_raw, list) or not 2 <= len(options_raw) <= 3:
                raise ValueError
            options: list[QuestionOption] = []
            option_ids: set[str] = set()
            option_labels: set[str] = set()
            for option in options_raw:
                if not isinstance(option, dict) or set(option) != {
                    "option_id",
                    "label",
                    "description",
                }:
                    raise ValueError
                option_id = _stored_identifier(option["option_id"], "option", max_length=128)
                label = _stored_public_text(
                    option["label"],
                    kind="option label",
                    maximum=config.max_option_label_chars,
                    raw_maximum=config.max_raw_text_chars,
                )
                if option_id in option_ids or label.casefold() in option_labels:
                    raise ValueError
                option_ids.add(option_id)
                option_labels.add(label.casefold())
                options.append(
                    QuestionOption(
                        option_id=option_id,
                        label=label,
                        description=_stored_public_text(
                            option["description"],
                            kind="option description",
                            maximum=config.max_option_description_chars,
                            raw_maximum=config.max_raw_text_chars,
                        ),
                    )
                )
            questions.append(
                StructuredQuestion(
                    question_id=question_id,
                    prompt=prompt,
                    options=tuple(options),
                )
            )
        return tuple(questions)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise StructuredStateStorageError("Stored question payload is invalid.") from None


def _question_set_from_row(row: sqlite3.Row, *, config: StructuredStateConfig) -> QuestionSet:
    questions = _questions_from_json(row["payload_json"], config=config)
    answers: tuple[QuestionAnswer, ...] = ()
    if row["answers_json"] is not None:
        try:
            decoded = json.loads(row["answers_json"])
            if not isinstance(decoded, dict):
                raise ValueError
            expected = {question.question_id for question in questions}
            if set(decoded) != expected or any(
                not isinstance(value, str) for value in decoded.values()
            ):
                raise ValueError
            answers = tuple(
                QuestionAnswer(
                    question_id=question.question_id, option_id=decoded[question.question_id]
                )
                for question in questions
            )
            for question, answer in zip(questions, answers, strict=True):
                if answer.option_id not in {option.option_id for option in question.options}:
                    raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise StructuredStateStorageError("Stored question answers are invalid.") from None
    try:
        question_set_id = _stored_identifier(row["question_set_id"], "question set", max_length=256)
        status = QuestionSetStatus(row["status"])
        revision = int(row["revision"])
        created_at = _stored_nonnegative_number(row["created_at"])
        updated_at = _stored_nonnegative_number(row["updated_at"])
        expires_at = _stored_nonnegative_number(row["expires_at"])
        terminal_at = (
            None if row["terminal_at"] is None else _stored_nonnegative_number(row["terminal_at"])
        )
        resolution_attempts = int(row["resolution_attempts"])
        if revision < 1 or resolution_attempts < 0 or updated_at < created_at:
            raise ValueError
        if expires_at <= created_at or (status is QuestionSetStatus.ANSWERED) != bool(answers):
            raise ValueError
        return QuestionSet(
            question_set_id=question_set_id,
            status=status,
            revision=revision,
            questions=questions,
            answers=answers,
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
            terminal_at=terminal_at,
            resolution_attempts=resolution_attempts,
        )
    except (TypeError, ValueError):
        raise StructuredStateStorageError("Stored question state is invalid.") from None


def _normalize_answers(
    answers: Mapping[str, str], questions: Sequence[StructuredQuestion]
) -> tuple[QuestionAnswer, ...]:
    expected = {question.question_id for question in questions}
    if any(not isinstance(key, str) for key in answers) or set(answers) != expected:
        raise StructuredStateValidationError("Answers must select one option for every question.")
    result: list[QuestionAnswer] = []
    for question in questions:
        option_id = _validate_identifier(
            answers[question.question_id], "selected option", max_length=128
        )
        if option_id not in {option.option_id for option in question.options}:
            raise StructuredStateValidationError("Selected question option is invalid.")
        result.append(QuestionAnswer(question_id=question.question_id, option_id=option_id))
    return tuple(result)


def _normalize_question_statuses(
    statuses: Iterable[QuestionSetStatus | str] | None,
) -> list[str]:
    if statuses is None:
        return []
    if isinstance(statuses, (str, bytes)):
        raise StructuredStateValidationError("Question-set statuses must be an array.")
    try:
        return list(dict.fromkeys(QuestionSetStatus(status).value for status in statuses))
    except (TypeError, ValueError):
        raise StructuredStateValidationError("Question-set status filter is invalid.") from None


def _public_text(value: Any, *, kind: str, maximum: int, raw_maximum: int) -> str:
    if not isinstance(value, str):
        raise StructuredStateValidationError(f"Structured {kind} must be a string.")
    if len(value) > raw_maximum:
        raise StructuredStateValidationError(f"Structured {kind} exceeds its input limit.")
    cleaned = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", value)).strip()
    if not cleaned:
        raise StructuredStateValidationError(f"Structured {kind} cannot be empty.")
    cleaned = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}={_REDACTED}", cleaned)
    cleaned = _SECRET_TOKEN_RE.sub(_REDACTED, cleaned)
    if len(cleaned) > maximum:
        cleaned = f"{cleaned[: max(1, maximum - 1)].rstrip()}…"
    return cleaned


def _stored_public_text(value: Any, *, kind: str, maximum: int, raw_maximum: int) -> str:
    if not isinstance(value, str):
        raise StructuredStateValidationError("Stored public text is invalid.")
    normalized = _public_text(
        value,
        kind=kind,
        maximum=maximum,
        raw_maximum=raw_maximum,
    )
    if normalized != value:
        raise StructuredStateValidationError("Stored public text is invalid.")
    return value


def _stored_identifier(value: Any, kind: str, *, max_length: int) -> str:
    return _validate_identifier(value, kind, max_length=max_length)


def _stored_nonnegative_number(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError
    return result


def _safe_deadline(now: float, duration: float) -> float:
    result = now + duration
    if not math.isfinite(result):
        raise StructuredStateValidationError("Structured-state deadline is invalid.")
    return result


def _validate_identifier(value: Any, kind: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise StructuredStateValidationError(f"Structured-state {kind} identifier is invalid.")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise StructuredStateValidationError(f"Structured-state {kind} identifier is invalid.")
    return value


def _validate_question_identifiers(session_id: Any, question_set_id: Any) -> tuple[str, str]:
    return (
        _validate_identifier(session_id, "session", max_length=256),
        _validate_identifier(question_set_id, "question set", max_length=256),
    )


def _validate_question_lease_identifiers(
    session_id: Any, question_set_id: Any, lease_token: Any
) -> tuple[str, str, str]:
    session, question_set = _validate_question_identifiers(session_id, question_set_id)
    return (
        session,
        question_set,
        _validate_identifier(lease_token, "question lease", max_length=256),
    )


def _validate_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StructuredStateValidationError("Task ledger revision is invalid.")
    return value


def _validate_duration(value: Any, *, minimum: float, maximum: float, kind: str) -> float:
    if isinstance(value, bool):
        raise StructuredStateValidationError(f"Structured-state {kind} is invalid.")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise StructuredStateValidationError(f"Structured-state {kind} is invalid.") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise StructuredStateValidationError(
            f"Structured-state {kind} is outside the allowed range."
        )
    return result


def _scope_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_matches(stored_hash: Any, token: str) -> bool:
    return isinstance(stored_hash, str) and hmac.compare_digest(stored_hash, _scope_digest(token))


def _is_transient_sqlite_error(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "nt" or not path.exists():
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


__all__ = [
    "DEFAULT_QUESTION_LEASE_SECONDS",
    "DurableStructuredState",
    "QuestionAnswer",
    "QuestionCreationResult",
    "QuestionIdempotencyConflict",
    "QuestionLeaseLost",
    "QuestionNotFound",
    "QuestionOption",
    "QuestionResolutionLease",
    "QuestionSet",
    "QuestionSetStatus",
    "QuestionStateError",
    "StructuredQuestion",
    "StructuredStateCapacityError",
    "StructuredStateConfig",
    "StructuredStateError",
    "StructuredStateStorageError",
    "StructuredStateValidationError",
    "TaskItem",
    "TaskLedger",
    "TaskRevisionConflict",
    "TaskStatus",
    "default_structured_state_path",
]
