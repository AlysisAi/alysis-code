from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier

import pytest

from alysis_code.ide.resumable_swarm import (
    DurableResumableSwarmCoordinator,
    ResumableSwarmConfig,
    SwarmCapacityError,
    SwarmFreshPermissionGrantRequired,
    SwarmIdempotencyConflict,
    SwarmJobNotFound,
    SwarmJobState,
    SwarmJobStateError,
    SwarmLeaseLost,
    SwarmPermissionScopeChanged,
    SwarmRevisionConflict,
    SwarmStorageError,
    SwarmUsage,
    SwarmValidationError,
)


@dataclass
class ManualClock:
    value: float = 10_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _scope(*, network: bool = False) -> dict[str, object]:
    return {
        "workspace_trusted": True,
        "mode": "work",
        "grants": ["workspace.write", *(["network"] if network else [])],
    }


def _fresh_grant(
    job_id: str,
    revision: int,
    *,
    session: str = "session-1",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "session_id": session,
        "job_id": job_id,
        "revision": revision,
    }


def _coordinator(
    tmp_path: Path,
    *,
    owner: str = "bridge-owner",
    workspace: Path | None = None,
    clock: ManualClock | None = None,
    path: Path | None = None,
    config: ResumableSwarmConfig | None = None,
) -> DurableResumableSwarmCoordinator:
    root = workspace or (tmp_path / "workspace")
    root.mkdir(parents=True, exist_ok=True)
    return DurableResumableSwarmCoordinator(
        owner_id=owner,
        workspace_root=root,
        path=path or (tmp_path / "private" / "resumable-swarm.sqlite3"),
        clock=clock or ManualClock(),
        config=config,
    )


def _start(
    coordinator: DurableResumableSwarmCoordinator,
    *,
    session: str = "session-1",
    key: str = "request-1",
    scope: dict[str, object] | None = None,
):
    return coordinator.start_job(
        session_id=session,
        idempotency_key=key,
        execution_spec={"plan_id": "plan-1", "task_ids": ["T01", "T02"]},
        permission_scope=scope or _scope(),
    )


def _claim(
    coordinator: DurableResumableSwarmCoordinator,
    job_id: str,
    *,
    worker: str = "worker-1",
    session: str = "session-1",
    scope: dict[str, object] | None = None,
    seconds: float = 30,
):
    return coordinator.claim_job(
        session_id=session,
        job_id=job_id,
        worker_id=worker,
        permission_scope=scope or _scope(),
        lease_seconds=seconds,
    )


def test_job_is_durable_idempotent_and_authority_is_not_public(tmp_path: Path) -> None:
    clock = ManualClock()
    path = tmp_path / "private" / "swarm.sqlite3"
    first = _coordinator(tmp_path, clock=clock, path=path)
    created = first.start_job(
        session_id="session-1",
        idempotency_key="request-sensitive",
        execution_spec={
            "plan_id": "plan-1",
            "api_key": "sk-production-super-secret",
            "note": "authorization: Bearer abcdefghijklmnop",
        },
        permission_scope=_scope(),
    )
    lease = _claim(first, created.status.job_id)

    reopened = _coordinator(tmp_path, clock=clock, path=path)
    status = reopened.get_status(session_id="session-1", job_id=created.status.job_id)
    public = json.dumps(status.public_payload(), sort_keys=True)

    assert created.created is True
    assert status.state is SwarmJobState.RUNNING
    assert lease.execution_spec["api_key"] == "[REDACTED]"
    assert "sk-production" not in repr(created)
    assert "sk-production" not in repr(lease)
    assert lease.lease_token not in repr(lease)
    assert lease.lease_token not in public
    assert "request-sensitive" not in public
    assert "permission_scope" not in public
    assert "execution_spec" not in public

    connection = sqlite3.connect(path)
    try:
        stored = connection.execute(
            """
            SELECT owner_key, idempotency_hash, execution_spec_json,
                permission_scope_sha256, lease_token_sha256, lease_worker_sha256
            FROM ide_swarm_jobs WHERE job_id = ?
            """,
            (created.status.job_id,),
        ).fetchone()
    finally:
        connection.close()
    assert stored is not None
    rendered_storage = " ".join(str(value) for value in stored)
    assert "bridge-owner" not in rendered_storage
    assert "request-sensitive" not in rendered_storage
    assert "worker-1" not in rendered_storage
    assert lease.lease_token not in rendered_storage
    assert "sk-production" not in rendered_storage
    assert "workspace.write" not in rendered_storage


def test_start_is_idempotent_but_rejects_changed_spec_or_scope(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    first = _start(coordinator)
    retry = _start(coordinator)
    assert retry.created is False
    assert retry.status.job_id == first.status.job_id

    with pytest.raises(SwarmIdempotencyConflict):
        coordinator.start_job(
            session_id="session-1",
            idempotency_key="request-1",
            execution_spec={"plan_id": "different"},
            permission_scope=_scope(),
        )
    with pytest.raises(SwarmIdempotencyConflict):
        coordinator.start_job(
            session_id="session-1",
            idempotency_key="request-1",
            execution_spec={"plan_id": "plan-1", "task_ids": ["T01", "T02"]},
            permission_scope=_scope(network=True),
        )


def test_owner_workspace_and_session_scopes_are_isolated(tmp_path: Path) -> None:
    path = tmp_path / "private" / "swarm.sqlite3"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    first = _coordinator(tmp_path, owner="owner-a", workspace=workspace_a, path=path)
    job = _start(first).status

    wrong_owner = _coordinator(tmp_path, owner="owner-b", workspace=workspace_a, path=path)
    wrong_workspace = _coordinator(tmp_path, owner="owner-a", workspace=workspace_b, path=path)
    for other in (wrong_owner, wrong_workspace):
        with pytest.raises(SwarmJobNotFound):
            other.get_status(session_id="session-1", job_id=job.job_id)
    with pytest.raises(SwarmJobNotFound):
        first.get_status(session_id="session-other", job_id=job.job_id)


def test_expired_worker_requires_explicit_resume_and_old_worker_is_fenced(tmp_path: Path) -> None:
    clock = ManualClock()
    path = tmp_path / "private" / "swarm.sqlite3"
    first_process = _coordinator(tmp_path, clock=clock, path=path)
    job = _start(first_process).status
    old_lease = _claim(first_process, job.job_id, seconds=5)
    first_process.record_usage(
        old_lease,
        SwarmUsage(calls=1, input_tokens=10, output_tokens=5, total_tokens=15),
        idempotency_key="provider-call-1",
    )

    clock.advance(5)
    restarted = _coordinator(tmp_path, clock=clock, path=path)
    recovered = restarted.recover_stale_jobs(session_id="session-1")
    assert len(recovered) == 1
    assert recovered[0].state is SwarmJobState.INTERRUPTED

    with pytest.raises(SwarmJobStateError):
        _claim(restarted, job.job_id, worker="worker-new")
    resumed = restarted.resume_job(
        session_id="session-1",
        job_id=job.job_id,
        permission_scope=_scope(),
        fresh_permission_grant=_fresh_grant(job.job_id, recovered[0].revision),
        expected_revision=recovered[0].revision,
    )
    assert resumed.state is SwarmJobState.QUEUED
    assert resumed.resume_count == 1
    new_lease = _claim(restarted, job.job_id, worker="worker-new")
    assert new_lease.generation > old_lease.generation
    assert new_lease.lease_token != old_lease.lease_token
    assert restarted.should_cancel(old_lease) is True
    with pytest.raises(SwarmLeaseLost):
        first_process.complete(old_lease, result={"clean": True})
    with pytest.raises(SwarmLeaseLost):
        first_process.record_usage(
            old_lease,
            SwarmUsage(calls=1, input_tokens=1, output_tokens=1, total_tokens=2),
            idempotency_key="late-provider-call",
        )

    final = restarted.complete(new_lease, result={"clean": True})
    assert final.status.state is SwarmJobState.SUCCEEDED
    assert final.status.attempts == 2
    assert final.status.usage.total_tokens == 15


def test_resume_can_atomically_take_over_an_expired_running_job(tmp_path: Path) -> None:
    clock = ManualClock()
    coordinator = _coordinator(tmp_path, clock=clock)
    job = _start(coordinator).status
    old_lease = _claim(coordinator, job.job_id, seconds=3)
    running = coordinator.get_status(session_id="session-1", job_id=job.job_id)
    clock.advance(3)

    resumed = coordinator.resume_job(
        session_id="session-1",
        job_id=job.job_id,
        permission_scope=_scope(),
        fresh_permission_grant=_fresh_grant(job.job_id, running.revision),
        expected_revision=running.revision,
    )

    assert resumed.state is SwarmJobState.QUEUED
    assert resumed.resume_count == 1
    assert coordinator.should_cancel(old_lease) is True
    new_lease = _claim(coordinator, job.job_id, worker="takeover-worker")
    assert new_lease.generation > old_lease.generation


def test_resume_fails_closed_when_permission_scope_changed(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id)
    interrupted = coordinator.interrupt(lease)

    with pytest.raises(SwarmFreshPermissionGrantRequired):
        coordinator.resume_job(
            session_id="session-1",
            job_id=job.job_id,
            permission_scope=_scope(),
            expected_revision=interrupted.revision,
        )

    with pytest.raises(SwarmPermissionScopeChanged):
        coordinator.resume_job(
            session_id="session-1",
            job_id=job.job_id,
            permission_scope=_scope(network=True),
            fresh_permission_grant=_fresh_grant(job.job_id, interrupted.revision),
            expected_revision=interrupted.revision,
        )

    unchanged = coordinator.get_status(session_id="session-1", job_id=job.job_id)
    assert unchanged.state is SwarmJobState.INTERRUPTED
    assert unchanged.resume_count == 0


def test_new_process_recovery_requires_a_fresh_matching_permission_grant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "private" / "swarm.sqlite3"
    environment = dict(os.environ)
    source_root = Path(__file__).resolve().parents[1] / "src"
    prior_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (os.fspath(source_root), prior_pythonpath) if part
    )
    create_script = textwrap.dedent(
        """
        import json
        import sys
        from alysis_code.ide.resumable_swarm import DurableResumableSwarmCoordinator

        workspace, database = sys.argv[1:3]
        scope = {"workspace_trusted": True, "recovery_grant": "plan-1"}
        coordinator = DurableResumableSwarmCoordinator(
            owner_id="bridge-owner", workspace_root=workspace, path=database
        )
        created = coordinator.start_job(
            session_id="session-1",
            idempotency_key="new-process-recovery",
            execution_spec={"plan_id": "plan-1"},
            permission_scope=scope,
        )
        lease = coordinator.claim_job(
            session_id="session-1",
            job_id=created.status.job_id,
            worker_id="worker-before-restart",
            permission_scope=scope,
        )
        interrupted = coordinator.interrupt(lease)
        print(json.dumps({"job_id": interrupted.job_id, "revision": interrupted.revision}))
        """
    )
    created_process = subprocess.run(  # noqa: S603 - isolated test interpreter
        [sys.executable, "-c", create_script, os.fspath(workspace), os.fspath(database)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    created = json.loads(created_process.stdout.strip().splitlines()[-1])

    resume_script = textwrap.dedent(
        """
        import json
        import sys
        from alysis_code.ide.resumable_swarm import (
            DurableResumableSwarmCoordinator,
            SwarmFreshPermissionGrantRequired,
        )

        workspace, database, job_id, raw_revision = sys.argv[1:5]
        revision = int(raw_revision)
        scope = {"workspace_trusted": True, "recovery_grant": "plan-1"}
        coordinator = DurableResumableSwarmCoordinator(
            owner_id="bridge-owner", workspace_root=workspace, path=database
        )
        denied_without_fresh_grant = False
        try:
            coordinator.resume_job(
                session_id="session-1",
                job_id=job_id,
                permission_scope=scope,
                expected_revision=revision,
            )
        except SwarmFreshPermissionGrantRequired:
            denied_without_fresh_grant = True
        unchanged = coordinator.get_status(session_id="session-1", job_id=job_id)
        resumed = coordinator.resume_job(
            session_id="session-1",
            job_id=job_id,
            permission_scope=scope,
            fresh_permission_grant={
                "schema_version": 1,
                "session_id": "session-1",
                "job_id": job_id,
                "revision": revision,
            },
            expected_revision=revision,
        )
        lease = coordinator.claim_job(
            session_id="session-1",
            job_id=job_id,
            worker_id="worker-after-restart",
            permission_scope=scope,
        )
        completed = coordinator.complete(lease, result={"clean": True})
        print(json.dumps({
            "denied_without_fresh_grant": denied_without_fresh_grant,
            "unchanged_state": unchanged.state.value,
            "resume_count": resumed.resume_count,
            "final_state": completed.status.state.value,
        }))
        """
    )
    resumed_process = subprocess.run(  # noqa: S603 - genuinely separate recovery interpreter
        [
            sys.executable,
            "-c",
            resume_script,
            os.fspath(workspace),
            os.fspath(database),
            created["job_id"],
            str(created["revision"]),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    recovered = json.loads(resumed_process.stdout.strip().splitlines()[-1])
    assert recovered == {
        "denied_without_fresh_grant": True,
        "unchanged_state": "interrupted",
        "resume_count": 1,
        "final_state": "succeeded",
    }


def test_claim_also_refreshes_permission_scope(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    job = _start(coordinator).status
    with pytest.raises(SwarmPermissionScopeChanged):
        _claim(coordinator, job.job_id, scope=_scope(network=True))
    assert (
        coordinator.get_status(session_id="session-1", job_id=job.job_id).state
        is SwarmJobState.QUEUED
    )


def test_cancel_fences_worker_before_returning_and_is_idempotent(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id)
    running = coordinator.get_status(session_id="session-1", job_id=job.job_id)

    cancelled = coordinator.cancel_job(
        session_id="session-1",
        job_id=job.job_id,
        expected_revision=running.revision,
    )
    retry = coordinator.cancel_job(session_id="session-1", job_id=job.job_id)

    assert cancelled.state is SwarmJobState.CANCELLED
    assert retry.revision == cancelled.revision
    assert coordinator.should_cancel(lease) is True
    with pytest.raises(SwarmLeaseLost):
        coordinator.complete(lease, result={"late": True})
    with pytest.raises(SwarmJobStateError):
        coordinator.resume_job(
            session_id="session-1",
            job_id=job.job_id,
            permission_scope=_scope(),
            fresh_permission_grant=_fresh_grant(job.job_id, cancelled.revision),
        )


def test_revision_compare_and_swap_rejects_stale_cancel_and_resume(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id)
    with pytest.raises(SwarmRevisionConflict) as cancel_conflict:
        coordinator.cancel_job(
            session_id="session-1",
            job_id=job.job_id,
            expected_revision=job.revision,
        )
    assert cancel_conflict.value.current_revision > job.revision

    interrupted = coordinator.interrupt(lease)
    with pytest.raises(SwarmRevisionConflict) as resume_conflict:
        coordinator.resume_job(
            session_id="session-1",
            job_id=job.job_id,
            permission_scope=_scope(),
            expected_revision=interrupted.revision - 1,
        )
    assert resume_conflict.value.current_revision == interrupted.revision


def test_usage_is_fenced_accumulated_and_validated(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id)

    coordinator.record_usage(
        lease,
        SwarmUsage(
            calls=1,
            input_tokens=100,
            output_tokens=30,
            cached_input_tokens=20,
            total_tokens=130,
        ),
        idempotency_key="provider-call-1",
    )
    status = coordinator.record_usage(
        lease,
        SwarmUsage(calls=2, input_tokens=5, output_tokens=5, total_tokens=10),
        idempotency_key="provider-call-2",
    )
    assert status.usage == SwarmUsage(
        calls=3,
        input_tokens=105,
        output_tokens=35,
        cached_input_tokens=20,
        total_tokens=140,
    )
    retry = coordinator.record_usage(
        lease,
        SwarmUsage(calls=2, input_tokens=5, output_tokens=5, total_tokens=10),
        idempotency_key="provider-call-2",
    )
    assert retry.usage == status.usage
    with pytest.raises(SwarmIdempotencyConflict):
        coordinator.record_usage(
            lease,
            SwarmUsage(calls=1, input_tokens=1, output_tokens=1, total_tokens=2),
            idempotency_key="provider-call-2",
        )

    with pytest.raises(SwarmValidationError):
        coordinator.record_usage(
            lease,
            SwarmUsage(calls=1, input_tokens=10, output_tokens=10, total_tokens=5),
            idempotency_key="bad-provider-call",
        )


def test_result_and_errors_are_bounded_redacted_and_durable(tmp_path: Path) -> None:
    coordinator = _coordinator(
        tmp_path,
        config=ResumableSwarmConfig(max_result_bytes=1024, max_string_chars=700),
    )
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id)
    secret = "sk-abcdefghijklmnopqrstuvwxyz"

    completed = coordinator.complete(
        lease,
        result={
            "summary": f"done with {secret}",
            "access_token": "never-store-this",
            "nested": {"authorization": "Bearer abcdefghijklmnop"},
            "token_usage": {"total_tokens": 42},
            "secret_values_included": False,
        },
    )
    public = json.dumps(completed.public_payload(), sort_keys=True)
    loaded = coordinator.get_result(session_id="session-1", job_id=job.job_id)

    assert secret not in public
    assert "never-store-this" not in public
    assert loaded.result == completed.result
    assert loaded.result is not None
    assert loaded.result["access_token"] == "[REDACTED]"
    assert loaded.result["token_usage"] == {"total_tokens": 42}
    assert loaded.result["secret_values_included"] is False

    second = _start(coordinator, key="request-2").status
    second_lease = _claim(coordinator, second.job_id)
    failed = coordinator.fail(
        second_lease,
        error_code="provider_failed",
        error_summary=f"token={secret} " + ("x" * 1000),
    )
    assert secret not in (failed.error_summary or "")
    assert len(failed.error_summary or "") <= coordinator.config.max_error_summary_chars

    third = _start(coordinator, key="request-3").status
    third_lease = _claim(coordinator, third.job_id)
    with pytest.raises(SwarmValidationError):
        coordinator.complete(third_lease, result={"too_large": "x" * 2000})
    assert (
        coordinator.get_status(session_id="session-1", job_id=third.job_id).state
        is SwarmJobState.RUNNING
    )


def test_only_one_concurrent_worker_can_claim_a_job(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    job = _start(coordinator).status
    barrier = Barrier(8)

    def claim(index: int) -> str:
        barrier.wait(timeout=10)
        try:
            return _claim(coordinator, job.job_id, worker=f"worker-{index}").lease_token
        except SwarmJobStateError:
            return "lost"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(8)))

    winners = [value for value in outcomes if value != "lost"]
    assert len(winners) == 1
    assert coordinator.get_status(session_id="session-1", job_id=job.job_id).attempts == 1


def test_two_restarted_processes_cannot_both_resume_one_expired_worker(tmp_path: Path) -> None:
    clock = ManualClock()
    path = tmp_path / "private" / "swarm.sqlite3"
    original = _coordinator(tmp_path, clock=clock, path=path)
    job = _start(original).status
    old_lease = _claim(original, job.job_id, seconds=2)
    running = original.get_status(session_id="session-1", job_id=job.job_id)
    clock.advance(2)
    first = _coordinator(tmp_path, clock=clock, path=path)
    second = _coordinator(tmp_path, clock=clock, path=path)
    barrier = Barrier(2)

    def resume(coordinator: DurableResumableSwarmCoordinator) -> str:
        barrier.wait(timeout=10)
        try:
            return coordinator.resume_job(
                session_id="session-1",
                job_id=job.job_id,
                permission_scope=_scope(),
                fresh_permission_grant=_fresh_grant(job.job_id, running.revision),
            ).state.value
        except (SwarmFreshPermissionGrantRequired, SwarmJobStateError):
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(resume, (first, second)))

    assert outcomes.count("queued") == 1
    assert outcomes.count("lost") == 1
    assert original.should_cancel(old_lease) is True


def test_cancel_and_complete_race_has_exactly_one_durable_winner(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id)
    barrier = Barrier(2)

    def complete() -> str:
        barrier.wait(timeout=10)
        try:
            return coordinator.complete(lease, result={"ok": True}).status.state.value
        except SwarmLeaseLost:
            return "fenced"

    def cancel() -> str:
        barrier.wait(timeout=10)
        try:
            return coordinator.cancel_job(session_id="session-1", job_id=job.job_id).state.value
        except SwarmJobStateError:
            return "already_terminal"

    with ThreadPoolExecutor(max_workers=2) as pool:
        complete_future = pool.submit(complete)
        cancel_future = pool.submit(cancel)
        outcomes = {complete_future.result(timeout=10), cancel_future.result(timeout=10)}

    final = coordinator.get_status(session_id="session-1", job_id=job.job_id)
    assert final.state in {SwarmJobState.SUCCEEDED, SwarmJobState.CANCELLED}
    if final.state is SwarmJobState.SUCCEEDED:
        assert outcomes == {"succeeded", "already_terminal"}
    else:
        assert outcomes == {"cancelled", "fenced"}


def test_renew_requires_live_lease_and_does_not_reuse_authority(tmp_path: Path) -> None:
    clock = ManualClock()
    coordinator = _coordinator(tmp_path, clock=clock)
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id, seconds=5)
    clock.advance(3)
    renewed = coordinator.renew(lease, lease_seconds=10)
    assert renewed.expires_at == clock.value + 10
    assert renewed.lease_token == lease.lease_token

    clock.advance(10)
    with pytest.raises(SwarmLeaseLost):
        coordinator.renew(renewed)


def test_failed_job_can_resume_but_succeeded_and_cancelled_jobs_cannot(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    failed_job = _start(coordinator).status
    failed_lease = _claim(coordinator, failed_job.job_id)
    failed = coordinator.fail(
        failed_lease,
        error_code="worker_failed",
        error_summary="Worker failed safely.",
    )
    resumed = coordinator.resume_job(
        session_id="session-1",
        job_id=failed_job.job_id,
        permission_scope=_scope(),
        fresh_permission_grant=_fresh_grant(failed_job.job_id, failed.revision),
        expected_revision=failed.revision,
    )
    assert resumed.state is SwarmJobState.QUEUED

    succeeded_job = _start(coordinator, key="succeeded").status
    succeeded_lease = _claim(coordinator, succeeded_job.job_id)
    succeeded = coordinator.complete(succeeded_lease, result={"ok": True}).status
    with pytest.raises(SwarmJobStateError):
        coordinator.resume_job(
            session_id="session-1",
            job_id=succeeded_job.job_id,
            permission_scope=_scope(),
            fresh_permission_grant=_fresh_grant(succeeded_job.job_id, succeeded.revision),
        )


def test_list_is_bounded_and_capacity_counts_only_outstanding(tmp_path: Path) -> None:
    coordinator = _coordinator(
        tmp_path,
        config=ResumableSwarmConfig(max_outstanding_per_session=2, max_list_limit=2),
    )
    first = _start(coordinator, key="one").status
    _start(coordinator, key="two")
    with pytest.raises(SwarmCapacityError):
        _start(coordinator, key="three")

    first_lease = _claim(coordinator, first.job_id)
    coordinator.complete(first_lease, result={"ok": True})
    third = _start(coordinator, key="three")
    listed = coordinator.list_jobs(session_id="session-1", limit=2)
    assert len(listed) == 2
    assert listed[0].job_id == third.status.job_id
    with pytest.raises(SwarmValidationError):
        coordinator.list_jobs(session_id="session-1", limit=3)


def test_storage_is_external_private_and_rejects_workspace_or_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SwarmValidationError, match="outside"):
        _coordinator(tmp_path, workspace=workspace, path=workspace / "state.sqlite3")

    real = tmp_path / "real.sqlite3"
    real.touch()
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("Symlink creation is not available for this Windows user.")
    with pytest.raises(SwarmValidationError, match="symlink"):
        _coordinator(tmp_path, workspace=workspace, path=link)


def test_storage_rejects_dangling_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = tmp_path / "dangling.sqlite3"
    try:
        link.symlink_to(tmp_path / "missing.sqlite3")
    except OSError:
        pytest.skip("Symlink creation is not available for this Windows user.")

    with pytest.raises(SwarmValidationError, match="symlink"):
        _coordinator(tmp_path, workspace=workspace, path=link)


def test_schema_version_and_corrupt_result_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "private" / "swarm.sqlite3"
    coordinator = _coordinator(tmp_path, path=path)
    job = _start(coordinator).status
    lease = _claim(coordinator, job.job_id)
    coordinator.complete(lease, result={"ok": True})

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE ide_swarm_jobs SET result_json = ? WHERE job_id = ?",
            ('{"access_token":"injected-secret"}', job.job_id),
        )
        connection.commit()
    finally:
        connection.close()
    sanitized = coordinator.get_result(session_id="session-1", job_id=job.job_id)
    assert sanitized.result == {"access_token": "[REDACTED]"}

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE ide_swarm_jobs SET result_json = ? WHERE job_id = ?",
            ("not-json", job.job_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SwarmStorageError):
        coordinator.get_result(session_id="session-1", job_id=job.job_id)

    other_path = tmp_path / "private" / "future.sqlite3"
    connection = sqlite3.connect(other_path)
    try:
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SwarmStorageError, match="schema"):
        _coordinator(tmp_path, path=other_path)


def test_invalid_json_shapes_and_identifiers_fail_before_storage(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    with pytest.raises(SwarmValidationError):
        coordinator.start_job(
            session_id="../escape",
            idempotency_key="request",
            execution_spec={"plan": "x"},
            permission_scope=_scope(),
        )
    with pytest.raises(SwarmValidationError):
        coordinator.start_job(
            session_id="session-1",
            idempotency_key="request",
            execution_spec={"bad": object()},
            permission_scope=_scope(),
        )
    with pytest.raises(SwarmValidationError):
        coordinator.start_job(
            session_id="session-1",
            idempotency_key="request",
            execution_spec={"bad": float("nan")},
            permission_scope=_scope(),
        )
