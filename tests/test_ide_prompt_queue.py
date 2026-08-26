from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier

import pytest

from alysis_code.ide.prompt_queue import (
    DurablePromptQueue,
    PromptIdempotencyConflict,
    PromptLeaseLost,
    PromptQueueCapacityError,
    PromptQueueConfig,
    PromptQueueStorageError,
    PromptQueueValidationError,
    PromptState,
    PromptStateError,
)


@dataclass
class ManualClock:
    value: float = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _queue(tmp_path: Path, clock: ManualClock | None = None, **config: int):
    return DurablePromptQueue(
        tmp_path / "state" / "prompt-queue.sqlite3",
        config=PromptQueueConfig(**config),
        clock=clock or ManualClock(),
    )


def _enqueue(
    queue: DurablePromptQueue,
    key: str,
    *,
    session: str = "session-1",
    message: str | None = None,
):
    return queue.enqueue(
        session_id=session,
        idempotency_key=key,
        payload={"message": message or f"message-{key}"},
    ).item


def test_enqueue_is_durable_ordered_and_idempotent_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "prompt-queue.sqlite3"
    clock = ManualClock()
    prompt_ids = iter(("prompt-one", "prompt-two"))
    first_queue = DurablePromptQueue(path, clock=clock, id_factory=lambda: next(prompt_ids))
    first = first_queue.enqueue(
        session_id="session-1",
        idempotency_key="request-1",
        payload={"message": "do not log this", "context": [{"type": "selection"}]},
    )
    clock.advance(1)
    second = first_queue.enqueue(
        session_id="session-1",
        idempotency_key="request-2",
        payload={"message": "next"},
    )

    reopened = DurablePromptQueue(path, clock=clock)
    retry = reopened.enqueue(
        session_id="session-1",
        idempotency_key="request-1",
        payload={"context": [{"type": "selection"}], "message": "do not log this"},
    )
    listed = reopened.list(session_id="session-1")

    assert first.created is True
    assert second.created is True
    assert retry.created is False
    assert retry.item.prompt_id == "prompt-one"
    assert [item.prompt_id for item in listed] == [first.item.prompt_id, second.item.prompt_id]
    assert listed[0].payload["message"] == "do not log this"


def test_idempotency_conflict_and_representations_do_not_reveal_content(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    secret = "sk-production-super-secret"
    result = queue.enqueue(
        session_id="session-1",
        idempotency_key="request-secret",
        payload={"message": secret},
    )
    lease = queue.claim_next(session_id="session-1", owner_id="bridge-1")
    assert lease is not None

    with pytest.raises(PromptIdempotencyConflict) as captured:
        queue.enqueue(
            session_id="session-1",
            idempotency_key="request-secret",
            payload={"message": f"different-{secret}"},
        )

    rendered = " ".join((repr(result), repr(lease), str(captured.value)))
    assert secret not in rendered
    assert "request-secret" not in rendered
    assert lease.lease_token not in rendered


def test_claim_serializes_a_session_and_preserves_order(tmp_path: Path) -> None:
    clock = ManualClock()
    queue = _queue(tmp_path, clock)
    first = _enqueue(queue, "request-1")
    second = _enqueue(queue, "request-2")

    first_lease = queue.claim_next(session_id="session-1", owner_id="bridge-1", lease_seconds=10)
    assert first_lease is not None
    assert first_lease.item.prompt_id == first.prompt_id
    assert first_lease.item.attempts == 1
    assert queue.claim_next(session_id="session-1", owner_id="bridge-2") is None

    completed = queue.complete(
        session_id="session-1",
        prompt_id=first.prompt_id,
        lease_token=first_lease.lease_token,
    )
    second_lease = queue.claim_next(session_id="session-1", owner_id="bridge-2")

    assert completed.state is PromptState.COMPLETED
    assert completed.terminal_at == clock.value
    assert second_lease is not None
    assert second_lease.item.prompt_id == second.prompt_id


def test_expired_claim_is_recovered_after_restart_and_old_token_is_fenced(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    path = tmp_path / "prompt-queue.sqlite3"
    first_process = DurablePromptQueue(path, clock=clock)
    item = _enqueue(first_process, "request-1")
    old_lease = first_process.claim_next(
        session_id="session-1", owner_id="bridge-old", lease_seconds=5
    )
    assert old_lease is not None

    clock.advance(5)
    restarted_process = DurablePromptQueue(path, clock=clock)
    recovered = restarted_process.claim_next(
        session_id="session-1", owner_id="bridge-new", lease_seconds=20
    )

    assert recovered is not None
    assert recovered.item.prompt_id == item.prompt_id
    assert recovered.item.attempts == 2
    assert recovered.lease_token != old_lease.lease_token
    with pytest.raises(PromptLeaseLost):
        first_process.complete(
            session_id="session-1",
            prompt_id=item.prompt_id,
            lease_token=old_lease.lease_token,
        )
    assert (
        restarted_process.complete(
            session_id="session-1",
            prompt_id=item.prompt_id,
            lease_token=recovered.lease_token,
        ).state
        is PromptState.COMPLETED
    )


def test_renew_and_release_require_a_live_fenced_lease(tmp_path: Path) -> None:
    clock = ManualClock()
    queue = _queue(tmp_path, clock)
    item = _enqueue(queue, "request-1")
    lease = queue.claim_next(session_id="session-1", owner_id="bridge-1", lease_seconds=5)
    assert lease is not None

    clock.advance(3)
    renewed = queue.renew(
        session_id="session-1",
        prompt_id=item.prompt_id,
        lease_token=lease.lease_token,
        lease_seconds=10,
    )
    assert renewed.expires_at == clock.value + 10

    released = queue.release(
        session_id="session-1",
        prompt_id=item.prompt_id,
        lease_token=lease.lease_token,
    )
    assert released.state is PromptState.PENDING
    assert released.attempts == 1
    with pytest.raises(PromptLeaseLost):
        queue.renew(
            session_id="session-1",
            prompt_id=item.prompt_id,
            lease_token=lease.lease_token,
        )
    claimed_again = queue.claim_next(session_id="session-1", owner_id="bridge-2")
    assert claimed_again is not None
    assert claimed_again.item.attempts == 2


def test_delete_only_cancels_pending_items_and_retains_audit_row(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    cancelled_item = _enqueue(queue, "request-cancel")
    running_item = _enqueue(queue, "request-run")
    cancelled = queue.delete_pending(session_id="session-1", prompt_id=cancelled_item.prompt_id)
    lease = queue.claim_next(session_id="session-1", owner_id="bridge-1")
    assert lease is not None
    assert lease.item.prompt_id == running_item.prompt_id

    assert cancelled.state is PromptState.CANCELLED
    assert queue.get(session_id="session-1", prompt_id=cancelled_item.prompt_id) == cancelled
    with pytest.raises(PromptStateError):
        queue.delete_pending(session_id="session-1", prompt_id=running_item.prompt_id)


def test_fail_records_only_a_bounded_error_code(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    item = _enqueue(queue, "request-1")
    lease = queue.claim_next(session_id="session-1", owner_id="bridge-1")
    assert lease is not None

    failed = queue.fail(
        session_id="session-1",
        prompt_id=item.prompt_id,
        lease_token=lease.lease_token,
        error_code="provider.timeout",
    )
    assert failed.state is PromptState.FAILED
    assert failed.error_code == "provider.timeout"
    with pytest.raises(PromptQueueValidationError):
        queue.fail(
            session_id="session-1",
            prompt_id=item.prompt_id,
            lease_token=lease.lease_token,
            error_code="secret details must not be persisted",
        )


def test_sessions_are_scoped_and_list_supports_state_and_cursor_filters(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    first = _enqueue(queue, "one", session="session-a")
    second = _enqueue(queue, "two", session="session-a")
    other = _enqueue(queue, "one", session="session-b")
    queue.delete_pending(session_id="session-a", prompt_id=first.prompt_id)

    pending = queue.list(session_id="session-a", states=[PromptState.PENDING])
    after = queue.list(session_id="session-a", after_sequence=first.sequence)

    assert [item.prompt_id for item in pending] == [second.prompt_id]
    assert [item.prompt_id for item in after] == [second.prompt_id]
    assert queue.get(session_id="session-a", prompt_id=other.prompt_id) is None
    assert [item.prompt_id for item in queue.list(session_id="session-b")] == [other.prompt_id]


def test_queue_enforces_payload_structure_size_depth_and_number_safety(tmp_path: Path) -> None:
    queue = _queue(tmp_path, max_payload_bytes=80, max_json_depth=2, max_json_nodes=10)
    invalid_payloads = (
        "not-an-object",
        {"message": object()},
        {"message": float("nan")},
        {1: "non-string-key"},
        {"a": {"b": {"c": "too-deep"}}},
        {"message": "x" * 200},
    )

    for index, payload in enumerate(invalid_payloads):
        with pytest.raises(PromptQueueValidationError):
            queue.enqueue(
                session_id="session-1",
                idempotency_key=f"invalid-{index}",
                payload=payload,  # type: ignore[arg-type]
            )
    assert queue.list(session_id="session-1") == []


def test_queue_capacity_bounds_outstanding_items_but_not_terminal_history(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path, max_outstanding_per_session=1)
    item = _enqueue(queue, "request-1")
    with pytest.raises(PromptQueueCapacityError):
        _enqueue(queue, "request-2")

    lease = queue.claim_next(session_id="session-1", owner_id="bridge-1")
    assert lease is not None
    queue.complete(session_id="session-1", prompt_id=item.prompt_id, lease_token=lease.lease_token)
    assert _enqueue(queue, "request-2").state is PromptState.PENDING


def test_concurrent_process_facades_cannot_claim_two_prompts_for_one_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prompt-queue.sqlite3"
    clock = ManualClock()
    creator = DurablePromptQueue(path, clock=clock)
    _enqueue(creator, "request-1")
    _enqueue(creator, "request-2")
    contenders = [DurablePromptQueue(path, clock=clock) for _ in range(8)]
    barrier = Barrier(len(contenders))

    def claim(index: int):
        barrier.wait()
        return contenders[index].claim_next(
            session_id="session-1", owner_id=f"bridge-{index}", lease_seconds=30
        )

    with ThreadPoolExecutor(max_workers=len(contenders)) as executor:
        results = list(executor.map(claim, range(len(contenders))))

    leases = [result for result in results if result is not None]
    assert len(leases) == 1
    assert leases[0].item.sequence == 1
    assert creator.list(session_id="session-1", states=["running"])[0].sequence == 1
    assert creator.list(session_id="session-1", states=["pending"])[0].sequence == 2


def test_concurrent_idempotent_enqueue_creates_one_row(tmp_path: Path) -> None:
    path = tmp_path / "prompt-queue.sqlite3"
    queues = [DurablePromptQueue(path) for _ in range(8)]
    barrier = Barrier(len(queues))

    def enqueue(index: int):
        barrier.wait()
        return queues[index].enqueue(
            session_id="session-1",
            idempotency_key="same-request",
            payload={"message": "same-content"},
        )

    with ThreadPoolExecutor(max_workers=len(queues)) as executor:
        results = list(executor.map(enqueue, range(len(queues))))

    assert sum(result.created for result in results) == 1
    assert len({result.item.prompt_id for result in results}) == 1
    assert len(queues[0].list(session_id="session-1")) == 1


def test_corrupt_or_future_database_fails_with_redacted_storage_error(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()

    with pytest.raises(PromptQueueStorageError) as captured:
        DurablePromptQueue(path)
    assert str(path) not in str(captured.value)


def test_version_one_queue_migrates_live_lease_ownership_and_execution_marker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE prompt_queue_items (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT,
            lease_owner TEXT,
            lease_expires_at REAL,
            terminal_at REAL,
            error_code TEXT,
            UNIQUE (session_id, idempotency_key)
        );
        INSERT INTO prompt_queue_items (
            prompt_id, session_id, idempotency_key, payload_json,
            payload_sha256, state, created_at, updated_at, attempts,
            lease_token, lease_owner, lease_expires_at
        ) VALUES (
            'legacy-prompt', 'legacy-session', 'legacy-send', '{"message":"legacy"}',
            'unused', 'running', 900, 900, 1,
            'legacy-token', 'legacy-bridge', 2000
        );
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    queue = DurablePromptQueue(path, clock=ManualClock())
    migrated = queue.get(session_id="legacy-session", prompt_id="legacy-prompt")
    assert migrated is not None
    assert migrated.execution_started_at is None
    renewed = queue.renew(
        session_id="legacy-session",
        prompt_id="legacy-prompt",
        lease_token="legacy-token",
        lease_seconds=10,
    )
    assert renewed.expires_at == 1_010
    started = queue.mark_execution_started(
        session_id="legacy-session",
        prompt_id="legacy-prompt",
        lease_token="legacy-token",
    )
    assert started.execution_started_at == 1_000


@pytest.mark.parametrize(
    ("kwargs", "method"),
    [
        ({"session_id": "has spaces", "idempotency_key": "key", "payload": {}}, "enqueue"),
        ({"session_id": "session", "idempotency_key": "", "payload": {}}, "enqueue"),
        ({"session_id": "session", "limit": 0}, "list"),
        ({"session_id": "session", "after_sequence": -1}, "list"),
        ({"session_id": "session", "states": "pending"}, "list"),
    ],
)
def test_public_inputs_are_bounded_and_typed(
    tmp_path: Path, kwargs: dict[str, object], method: str
) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(PromptQueueValidationError):
        getattr(queue, method)(**kwargs)


def test_cancel_claimed_requires_live_fencing_token(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    item = _enqueue(queue, "request-1")
    lease = queue.claim_next(session_id="session-1", owner_id="bridge-1")
    assert lease is not None

    cancelled = queue.cancel_claimed(
        session_id="session-1",
        prompt_id=item.prompt_id,
        lease_token=lease.lease_token,
    )

    assert cancelled.state is PromptState.CANCELLED
    with pytest.raises(PromptLeaseLost):
        queue.cancel_claimed(
            session_id="session-1",
            prompt_id=item.prompt_id,
            lease_token=lease.lease_token,
        )


def test_cancel_pending_for_session_is_atomic_and_session_scoped(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    first = _enqueue(queue, "request-1")
    second = _enqueue(queue, "request-2")
    other = _enqueue(queue, "request-3", session="other-session")
    lease = queue.claim_next(session_id="session-1", owner_id="bridge-1")
    assert lease is not None and lease.item.prompt_id == first.prompt_id

    assert queue.cancel_pending_for_session(session_id="session-1") == 1
    assert queue.get(session_id="session-1", prompt_id=first.prompt_id).state is PromptState.RUNNING
    assert (
        queue.get(session_id="session-1", prompt_id=second.prompt_id).state is PromptState.CANCELLED
    )
    assert (
        queue.get(session_id="other-session", prompt_id=other.prompt_id).state
        is PromptState.PENDING
    )


def test_rebind_moves_logical_ownership_without_stealing_a_live_lease(tmp_path: Path) -> None:
    clock = ManualClock()
    queue = _queue(tmp_path, clock)
    first = _enqueue(queue, "request-1", session="old-session")
    second = _enqueue(queue, "request-2", session="old-session")
    lease = queue.claim_next(session_id="old-session", owner_id="old-bridge", lease_seconds=10)
    assert lease is not None and lease.item.prompt_id == first.prompt_id

    before_expiry = queue.rebind_recoverable(
        source_session_id="old-session", target_session_id="new-session"
    )
    assert before_expiry == {"pending": 1, "recovered": 0, "active": 1}
    assert [item.prompt_id for item in queue.list(session_id="new-session")] == [
        first.prompt_id,
        second.prompt_id,
    ]
    assert queue.claim_next(session_id="new-session", owner_id="new-bridge") is None

    completed = queue.complete(
        session_id="old-session",
        prompt_id=first.prompt_id,
        lease_token=lease.lease_token,
    )
    assert completed.session_id == "new-session"
    assert completed.state is PromptState.COMPLETED
    next_lease = queue.claim_next(session_id="new-session", owner_id="new-bridge")
    assert next_lease is not None and next_lease.item.prompt_id == second.prompt_id


def test_rebind_recovers_expired_lease_but_preserves_execution_marker(tmp_path: Path) -> None:
    clock = ManualClock()
    queue = _queue(tmp_path, clock)
    item = _enqueue(queue, "request-1", session="old-session")
    lease = queue.claim_next(session_id="old-session", owner_id="old-bridge", lease_seconds=10)
    assert lease is not None
    started = queue.mark_execution_started(
        session_id="old-session",
        prompt_id=item.prompt_id,
        lease_token=lease.lease_token,
    )
    assert started.execution_started_at == clock.value

    clock.advance(11)
    recovered = queue.rebind_recoverable(
        source_session_id="old-session", target_session_id="new-session"
    )
    assert recovered == {"pending": 0, "recovered": 1, "active": 0}
    rebound = queue.get(session_id="new-session", prompt_id=item.prompt_id)
    assert rebound is not None
    assert rebound.state is PromptState.PENDING
    assert rebound.execution_started_at == started.execution_started_at
    new_lease = queue.claim_next(session_id="new-session", owner_id="new-bridge")
    assert new_lease is not None
    with pytest.raises(PromptLeaseLost):
        queue.complete(
            session_id="old-session",
            prompt_id=item.prompt_id,
            lease_token=lease.lease_token,
        )
