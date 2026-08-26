from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alysis_code.ide.forge_request_ledger import (
    DurableForgeRequestLedger,
    ForgeRequestIdempotencyConflict,
    ForgeRequestLeaseLost,
    ForgeRequestLedgerConfig,
    ForgeRequestState,
)


class ManualClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.value += seconds


def _ledger(tmp_path: Path, clock: ManualClock | None = None) -> DurableForgeRequestLedger:
    return DurableForgeRequestLedger(
        tmp_path / "private" / "forge.sqlite3",
        config=ForgeRequestLedgerConfig(lease_seconds=2.0),
        clock=clock or ManualClock(),
    )


def _accept(
    ledger: DurableForgeRequestLedger,
    workspace: Path,
    *,
    key: str = "request-123",
    instruction: str = "Build the secret launch workflow",
):
    return ledger.accept(
        workspace_root=workspace,
        session_id="session-1",
        idempotency_key=key,
        payload={"instruction": instruction, "mode": "review"},
    )


def test_accept_is_stable_hashed_and_detects_payload_conflicts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = _ledger(tmp_path)

    first = _accept(ledger, workspace)
    duplicate = _accept(ledger, workspace)

    assert first.created is True
    assert first.dispatch_lease is not None
    assert duplicate.created is False
    assert duplicate.dispatch_lease is None
    assert duplicate.record.job_id == first.record.job_id
    with pytest.raises(ForgeRequestIdempotencyConflict, match="different Forge plan request"):
        _accept(ledger, workspace, instruction="Delete something else")

    raw_database = ledger.path.read_bytes()
    assert b"request-123" not in raw_database
    assert b"secret launch workflow" not in raw_database
    assert b"Delete something else" not in raw_database
    if os.name != "nt":
        assert ledger.path.stat().st_mode & 0o077 == 0


def test_concurrent_acceptance_has_one_job_and_one_dispatch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "private" / "forge.sqlite3"
    ledgers = [DurableForgeRequestLedger(path) for _ in range(8)]
    gate = threading.Barrier(len(ledgers))

    def accept(index: int):
        gate.wait()
        return _accept(ledgers[index], workspace)

    with ThreadPoolExecutor(max_workers=len(ledgers)) as pool:
        results = list(pool.map(accept, range(len(ledgers))))

    assert len({result.record.job_id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    assert sum(result.dispatch_lease is not None for result in results) == 1


def test_expired_queued_dispatch_is_fenced_and_safely_reclaimed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clock = ManualClock()
    ledger = _ledger(tmp_path, clock)
    first = _accept(ledger, workspace)
    assert first.dispatch_lease is not None

    clock.advance(3.0)
    retried = _accept(ledger, workspace)
    assert retried.record.job_id == first.record.job_id
    assert retried.dispatch_lease is not None
    assert retried.dispatch_lease.generation == first.dispatch_lease.generation + 1
    with pytest.raises(ForgeRequestLeaseLost):
        ledger.begin(first.dispatch_lease)

    worker = ledger.begin(retried.dispatch_lease)
    assert ledger.get(worker.job_id).state is ForgeRequestState.RUNNING


def test_expired_running_worker_is_indeterminate_and_never_reexecuted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clock = ManualClock()
    ledger = _ledger(tmp_path, clock)
    accepted = _accept(ledger, workspace)
    assert accepted.dispatch_lease is not None
    worker = ledger.begin(accepted.dispatch_lease)

    clock.advance(3.0)
    recovered = DurableForgeRequestLedger(
        ledger.path,
        config=ForgeRequestLedgerConfig(lease_seconds=2.0),
        clock=clock,
    )
    status = recovered.get(worker.job_id)
    assert status is not None
    assert status.state is ForgeRequestState.INDETERMINATE
    assert status.error_code == "worker_lease_expired"

    retry = _accept(recovered, workspace)
    assert retry.record.job_id == worker.job_id
    assert retry.record.state is ForgeRequestState.INDETERMINATE
    assert retry.dispatch_lease is None
    with pytest.raises(ForgeRequestLeaseLost):
        ledger.complete(worker, plan_id="plan-1")


def test_heartbeat_and_terminal_result_survive_reopen(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clock = ManualClock()
    ledger = _ledger(tmp_path, clock)
    accepted = _accept(ledger, workspace)
    assert accepted.dispatch_lease is not None
    worker = ledger.begin(accepted.dispatch_lease)

    clock.advance(1.5)
    renewed = ledger.renew(worker)
    clock.advance(1.5)
    completed = ledger.complete(renewed, plan_id="plan-123")
    assert completed.state is ForgeRequestState.COMPLETED

    reopened = _ledger(tmp_path, clock)
    status = reopened.get(worker.job_id)
    assert status is not None
    assert status.state is ForgeRequestState.COMPLETED
    assert status.plan_id == "plan-123"
    duplicate = _accept(reopened, workspace)
    assert duplicate.record.job_id == worker.job_id
    assert duplicate.dispatch_lease is None
