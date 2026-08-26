from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path
from threading import Barrier

import pytest

from alysis_code.ide.structured_state import (
    DurableStructuredState,
    QuestionIdempotencyConflict,
    QuestionLeaseLost,
    QuestionSetStatus,
    QuestionStateError,
    StructuredStateCapacityError,
    StructuredStateConfig,
    StructuredStateStorageError,
    StructuredStateValidationError,
    TaskRevisionConflict,
    TaskStatus,
    default_structured_state_path,
)


@dataclass
class ManualClock:
    value: float = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _store(
    tmp_path: Path,
    *,
    clock: ManualClock | None = None,
    owner: str = "local-owner",
    workspace: Path | None = None,
    database: Path | None = None,
    config: StructuredStateConfig | None = None,
    ids: list[str] | None = None,
    leases: list[str] | None = None,
) -> DurableStructuredState:
    workspace = workspace or (tmp_path / "workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    id_values = iter(ids) if ids is not None else None
    lease_values = iter(leases) if leases is not None else None
    return DurableStructuredState(
        owner_id=owner,
        workspace_root=workspace,
        path=database or (tmp_path / "state" / "structured.sqlite3"),
        clock=clock or ManualClock(),
        config=config,
        id_factory=(None if id_values is None else lambda: next(id_values)),
        lease_factory=(None if lease_values is None else lambda: next(lease_values)),
    )


def _questions(secret: str = "") -> list[dict[str, object]]:
    suffix = f" token={secret}" if secret else ""
    return [
        {
            "question_id": "strategy",
            "prompt": f"Choose a delivery strategy{suffix}",
            "options": [
                {
                    "option_id": "safe",
                    "label": "Safe",
                    "description": f"Prefer verification{suffix}",
                },
                {
                    "option_id": "fast",
                    "label": "Fast",
                    "description": "Prefer throughput",
                },
            ],
        }
    ]


def _create(
    store: DurableStructuredState,
    *,
    session: str = "session-1",
    key: str = "request-1",
    ttl: float = 60,
):
    return store.create_question_set(
        session_id=session,
        idempotency_key=key,
        questions=_questions(),
        expires_in_seconds=ttl,
    ).question_set


def _process_task_replace(database: str, workspace: str, task_id: str) -> str:
    store = DurableStructuredState(
        owner_id="process-owner",
        workspace_root=workspace,
        path=database,
    )
    try:
        store.replace_tasks(
            session_id="process-session",
            expected_revision=0,
            tasks=[{"task_id": task_id, "title": task_id, "status": "in_progress"}],
        )
    except TaskRevisionConflict:
        return "conflict"
    return "updated"


def test_default_path_honors_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "private-data"
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(data_dir))

    assert default_structured_state_path() == data_dir / "ide" / "structured-state.sqlite3"


def test_storage_must_be_external_and_not_a_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(StructuredStateValidationError, match="outside"):
        _store(tmp_path, workspace=workspace, database=workspace / "state.sqlite3")

    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "state-link.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks are unavailable on this host")
    with pytest.raises(StructuredStateValidationError, match="symlink"):
        _store(tmp_path, workspace=workspace, database=link)


def test_storage_rejects_dangling_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = tmp_path / "dangling.sqlite3"
    try:
        link.symlink_to(tmp_path / "missing.sqlite3")
    except OSError:
        pytest.skip("Symlinks are unavailable on this host")

    with pytest.raises(StructuredStateValidationError, match="symlink"):
        _store(tmp_path, workspace=workspace, database=link)


def test_invalid_workspace_path_has_a_safe_error(tmp_path: Path) -> None:
    missing = tmp_path / "private-workspace-name"
    with pytest.raises(StructuredStateValidationError) as captured:
        DurableStructuredState(owner_id="owner", workspace_root=missing, path=tmp_path / "state.db")
    assert str(missing) not in str(captured.value)


def test_task_ledger_is_durable_ordered_redacted_and_bounded(tmp_path: Path) -> None:
    clock = ManualClock()
    database = tmp_path / "state.sqlite3"
    first = _store(tmp_path, clock=clock, database=database)
    secret = "sk-production-secret-value"

    written = first.replace_tasks(
        session_id="session-1",
        expected_revision=0,
        tasks=[
            {"task_id": "inspect", "title": f"Inspect token={secret}", "status": "completed"},
            {"task_id": "build", "title": "B" * 500, "status": "in_progress"},
        ],
    )
    reopened = _store(tmp_path, clock=clock, database=database)
    loaded = reopened.get_task_ledger(session_id="session-1")

    assert loaded == written
    assert loaded.revision == 1
    assert [task.task_id for task in loaded.tasks] == ["inspect", "build"]
    assert secret not in repr(loaded)
    assert secret not in str(loaded.public_payload())
    assert "[REDACTED]" in loaded.tasks[0].title
    assert len(loaded.tasks[1].title) <= first.config.max_task_title_chars
    assert loaded.tasks[1].title.endswith("…")


def test_task_replace_uses_revision_cas_across_store_instances(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    first = _store(tmp_path, database=database)
    second = _store(tmp_path, database=database)
    barrier = Barrier(2)

    def update(store: DurableStructuredState, task_id: str) -> str:
        barrier.wait()
        try:
            store.replace_tasks(
                session_id="session-1",
                expected_revision=0,
                tasks=[{"task_id": task_id, "title": task_id, "status": "in_progress"}],
            )
        except TaskRevisionConflict:
            return "conflict"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: update(*args), [(first, "one"), (second, "two")]))

    assert sorted(outcomes) == ["conflict", "updated"]
    assert first.get_task_ledger(session_id="session-1").revision == 1
    with pytest.raises(TaskRevisionConflict) as captured:
        first.replace_tasks(session_id="session-1", expected_revision=0, tasks=[])
    assert captured.value.current_revision == 1


def test_task_revision_cas_is_process_safe(tmp_path: Path) -> None:
    database = tmp_path / "process-state.sqlite3"
    workspace = tmp_path / "process-workspace"
    workspace.mkdir()
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        outcomes = list(
            pool.map(
                _process_task_replace,
                [str(database), str(database)],
                [str(workspace), str(workspace)],
                ["process-one", "process-two"],
            )
        )

    assert sorted(outcomes) == ["conflict", "updated"]
    final = DurableStructuredState(
        owner_id="process-owner", workspace_root=workspace, path=database
    ).get_task_ledger(session_id="process-session")
    assert final.revision == 1


@pytest.mark.parametrize(
    "tasks, error",
    [
        (
            [
                {"task_id": "one", "title": "One", "status": "in_progress"},
                {"task_id": "two", "title": "Two", "status": "in_progress"},
            ],
            "Only one",
        ),
        (
            [
                {"task_id": "same", "title": "One", "status": "pending"},
                {"task_id": "same", "title": "Two", "status": "blocked"},
            ],
            "unique",
        ),
        ([{"task_id": "one", "title": "One", "status": "running"}], "status"),
    ],
)
def test_task_ledger_rejects_invalid_state(
    tmp_path: Path, tasks: list[dict[str, str]], error: str
) -> None:
    with pytest.raises(StructuredStateValidationError, match=error):
        _store(tmp_path).replace_tasks(session_id="session-1", expected_revision=0, tasks=tasks)


def test_task_capacity_is_enforced_before_storage(tmp_path: Path) -> None:
    store = _store(tmp_path, config=StructuredStateConfig(max_tasks_per_session=1))
    with pytest.raises(StructuredStateCapacityError):
        store.replace_tasks(
            session_id="session-1",
            expected_revision=0,
            tasks=[
                {"task_id": "one", "title": "One", "status": "pending"},
                {"task_id": "two", "title": "Two", "status": "pending"},
            ],
        )
    assert store.get_task_ledger(session_id="session-1").revision == 0


def test_owner_workspace_and_session_scopes_are_isolated(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    first = _store(tmp_path, database=database, workspace=workspace_one, owner="owner-one")
    first.replace_tasks(
        session_id="session-1",
        expected_revision=0,
        tasks=[{"task_id": "private", "title": "Private", "status": "pending"}],
    )
    question = _create(first)

    same_scope = _store(tmp_path, database=database, workspace=workspace_one, owner="owner-one")
    other_owner = _store(tmp_path, database=database, workspace=workspace_one, owner="owner-two")
    other_workspace = _store(
        tmp_path, database=database, workspace=workspace_two, owner="owner-one"
    )

    assert same_scope.get_task_ledger(session_id="session-1").tasks[0].task_id == "private"
    assert (
        same_scope.get_question_set(
            session_id="session-1", question_set_id=question.question_set_id
        )
        is not None
    )
    for scoped_store in (other_owner, other_workspace):
        assert scoped_store.get_task_ledger(session_id="session-1").tasks == ()
        assert (
            scoped_store.get_question_set(
                session_id="session-1", question_set_id=question.question_set_id
            )
            is None
        )
    assert first.get_task_ledger(session_id="session-2").tasks == ()


def test_question_creation_is_durable_idempotent_redacted_and_secret_free(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    secret = "sk-production-secret-value"
    first = _store(tmp_path, database=database, ids=["questions-1"])
    created = first.create_question_set(
        session_id="session-1",
        idempotency_key="operation-opaque",
        questions=_questions(secret),
        expires_in_seconds=60,
    )
    reopened = _store(tmp_path, database=database)
    retried = reopened.create_question_set(
        session_id="session-1",
        idempotency_key="operation-opaque",
        questions=_questions(secret),
        expires_in_seconds=60,
    )

    assert created.created is True
    assert retried.created is False
    assert retried.question_set == created.question_set
    rendered = repr(created) + str(created.question_set.public_payload())
    assert secret not in rendered
    assert "[REDACTED]" in rendered
    database_bytes = database.read_bytes()
    wal = database.with_name(f"{database.name}-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    assert secret.encode() not in database_bytes
    assert b"operation-opaque" not in database_bytes


def test_question_idempotency_conflict_does_not_reveal_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_question_set(
        session_id="session-1",
        idempotency_key="same-key",
        questions=_questions(),
        expires_in_seconds=60,
    )
    secret = "sk-do-not-leak-this-value"
    with pytest.raises(QuestionIdempotencyConflict) as captured:
        store.create_question_set(
            session_id="session-1",
            idempotency_key="same-key",
            questions=_questions(secret),
            expires_in_seconds=60,
        )
    assert secret not in str(captured.value)
    assert "same-key" not in str(captured.value)


@pytest.mark.parametrize(
    "questions, match",
    [
        ([], "one to three"),
        (_questions() * 4, "one to three"),
        (
            [
                {
                    "question_id": "q",
                    "prompt": "Choose",
                    "options": [{"option_id": "one", "label": "One", "description": "One"}],
                }
            ],
            "two or three",
        ),
        (
            [
                {
                    "question_id": "q",
                    "prompt": "Choose",
                    "options": [
                        {"option_id": "one", "label": "Same", "description": "One"},
                        {"option_id": "two", "label": "same", "description": "Two"},
                    ],
                }
            ],
            "distinct",
        ),
    ],
)
def test_question_shape_is_strict(
    tmp_path: Path, questions: list[dict[str, object]], match: str
) -> None:
    with pytest.raises(StructuredStateValidationError, match=match):
        _store(tmp_path).create_question_set(
            session_id="session-1",
            idempotency_key="request-1",
            questions=questions,
            expires_in_seconds=60,
        )


def test_claim_answer_is_fenced_validated_and_one_shot(tmp_path: Path) -> None:
    store = _store(tmp_path, leases=["secret-resolution-token"])
    question_set = _create(store)
    lease = store.claim_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        resolver_id="webview-1",
        lease_seconds=10,
    )
    assert lease is not None
    assert lease.question_set.status is QuestionSetStatus.RESOLVING
    assert lease.lease_token not in repr(lease)
    persisted = store.path.read_bytes()
    wal = store.path.with_name(f"{store.path.name}-wal")
    if wal.exists():
        persisted += wal.read_bytes()
    assert b"secret-resolution-token" not in persisted
    assert b"webview-1" not in persisted
    with pytest.raises(StructuredStateValidationError, match="every question"):
        store.answer_question_set(
            session_id="session-1",
            question_set_id=question_set.question_set_id,
            lease_token=lease.lease_token,
            answers={},
        )
    with pytest.raises(StructuredStateValidationError, match="option"):
        store.answer_question_set(
            session_id="session-1",
            question_set_id=question_set.question_set_id,
            lease_token=lease.lease_token,
            answers={"strategy": "unknown"},
        )

    answered = store.answer_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        lease_token=lease.lease_token,
        answers={"strategy": "safe"},
    )
    assert answered.status is QuestionSetStatus.ANSWERED
    assert answered.answers[0].option_id == "safe"
    with pytest.raises(QuestionLeaseLost):
        store.answer_question_set(
            session_id="session-1",
            question_set_id=question_set.question_set_id,
            lease_token=lease.lease_token,
            answers={"strategy": "fast"},
        )
    with pytest.raises(QuestionStateError):
        store.cancel_question_set(
            session_id="session-1", question_set_id=question_set.question_set_id
        )


def test_only_one_concurrent_question_resolver_wins(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    seed = _store(tmp_path, database=database)
    question_set = _create(seed)
    first = _store(tmp_path, database=database)
    second = _store(tmp_path, database=database)
    barrier = Barrier(2)

    def claim(store: DurableStructuredState, resolver: str) -> bool:
        barrier.wait()
        return (
            store.claim_question_set(
                session_id="session-1",
                question_set_id=question_set.question_set_id,
                resolver_id=resolver,
                lease_seconds=10,
            )
            is not None
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: claim(*args), [(first, "one"), (second, "two")]))
    assert sorted(outcomes) == [False, True]


def test_expired_resolution_recovers_after_restart_and_fences_old_process(tmp_path: Path) -> None:
    clock = ManualClock()
    database = tmp_path / "state.sqlite3"
    old = _store(
        tmp_path,
        database=database,
        clock=clock,
        leases=["old-token"],
    )
    question_set = _create(old)
    old_lease = old.claim_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        resolver_id="old-process",
        lease_seconds=5,
    )
    assert old_lease is not None
    clock.advance(5)
    restarted = _store(
        tmp_path,
        database=database,
        clock=clock,
        leases=["new-token"],
    )
    new_lease = restarted.claim_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        resolver_id="new-process",
        lease_seconds=10,
    )
    assert new_lease is not None
    assert new_lease.question_set.resolution_attempts == 2
    with pytest.raises(QuestionLeaseLost):
        old.answer_question_set(
            session_id="session-1",
            question_set_id=question_set.question_set_id,
            lease_token=old_lease.lease_token,
            answers={"strategy": "safe"},
        )
    answered = restarted.answer_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        lease_token=new_lease.lease_token,
        answers={"strategy": "fast"},
    )
    assert answered.answers[0].option_id == "fast"


def test_question_expiry_is_terminal_and_clears_resolution(tmp_path: Path) -> None:
    clock = ManualClock()
    store = _store(tmp_path, clock=clock)
    question_set = _create(store, ttl=5)
    clock.advance(5)

    expired = store.get_question_set(
        session_id="session-1", question_set_id=question_set.question_set_id
    )
    assert expired is not None
    assert expired.status is QuestionSetStatus.EXPIRED
    assert expired.terminal_at == clock.value
    with pytest.raises(QuestionStateError):
        store.claim_question_set(
            session_id="session-1",
            question_set_id=question_set.question_set_id,
            resolver_id="resolver",
        )


def test_renew_release_and_cancel_obey_lease_fencing(tmp_path: Path) -> None:
    clock = ManualClock()
    store = _store(tmp_path, clock=clock, leases=["token-one", "token-two"])
    question_set = _create(store)
    first = store.claim_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        resolver_id="resolver-one",
        lease_seconds=5,
    )
    assert first is not None
    clock.advance(2)
    renewed = store.renew_question_lease(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        lease_token=first.lease_token,
        lease_seconds=10,
    )
    assert renewed.expires_at == clock.value + 10
    pending = store.release_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        lease_token=first.lease_token,
    )
    assert pending.status is QuestionSetStatus.PENDING
    with pytest.raises(QuestionLeaseLost):
        store.cancel_question_set(
            session_id="session-1",
            question_set_id=question_set.question_set_id,
            lease_token=first.lease_token,
        )
    second = store.claim_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        resolver_id="resolver-two",
    )
    assert second is not None
    with pytest.raises(QuestionLeaseLost):
        store.cancel_question_set(
            session_id="session-1",
            question_set_id=question_set.question_set_id,
            lease_token=first.lease_token,
        )
    cancelled = store.cancel_question_set(
        session_id="session-1",
        question_set_id=question_set.question_set_id,
        lease_token=second.lease_token,
    )
    assert cancelled.status is QuestionSetStatus.CANCELLED


def test_pending_question_can_be_cancelled_without_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    question_set = _create(store)
    cancelled = store.cancel_question_set(
        session_id="session-1", question_set_id=question_set.question_set_id
    )
    assert cancelled.status is QuestionSetStatus.CANCELLED


def test_capacity_counts_only_live_question_sets(tmp_path: Path) -> None:
    clock = ManualClock()
    store = _store(
        tmp_path,
        clock=clock,
        config=StructuredStateConfig(max_question_sets_per_session=1),
    )
    first = _create(store, key="one", ttl=5)
    with pytest.raises(StructuredStateCapacityError):
        _create(store, key="two")
    clock.advance(5)
    second = _create(store, key="two")
    assert second.question_set_id != first.question_set_id


def test_invalid_database_schema_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(StructuredStateStorageError, match="not supported"):
        _store(tmp_path, workspace=workspace, database=database)


def test_tampered_public_question_payload_fails_closed_without_leaking(tmp_path: Path) -> None:
    store = _store(tmp_path)
    question_set = _create(store)
    secret = "sk-tampered-secret-value"
    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE ide_question_sets SET payload_json = ? WHERE question_set_id = ?",
        (
            '[{"options":[{"description":"One","label":"One","option_id":"one"},'
            '{"description":"Two","label":"Two","option_id":"two"}],'
            f'"prompt":"Use {secret}","question_id":"strategy"}}]',
            question_set.question_set_id,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(StructuredStateStorageError) as captured:
        store.get_question_set(session_id="session-1", question_set_id=question_set.question_set_id)
    assert secret not in str(captured.value)


def test_public_status_values_are_stable() -> None:
    assert {status.value for status in TaskStatus} == {
        "pending",
        "in_progress",
        "completed",
        "blocked",
    }
    assert {status.value for status in QuestionSetStatus} == {
        "pending",
        "resolving",
        "answered",
        "cancelled",
        "expired",
    }
