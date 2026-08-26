"""Recovery from an execution that died without cleaning up after itself.

The centrepiece drives a *real* second process: it takes the same workspace locks
a `forge exec` takes, marks the run running, and is then hard-killed. Nothing is
simulated about the residue -- the lock files, the ``running`` pointer and the
``in_progress`` task are exactly what a `kill -9` leaves behind -- so the asserts
below are about the recovery path, not about a fixture's idea of one.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.forge import (
    add_task,
    create_plan_run,
    current_run_pointer_path,
    current_run_status,
    load_current_run_paths,
    load_plan,
    save_plan,
)
from alysis_code.forge_events import parse_event_line
from alysis_code.run_lock import (
    STALENESS_ACTIVE,
    STALENESS_AMBIGUOUS,
    STALENESS_STALE,
    assess_lock_staleness,
    describe_run_mutation_lock,
)
from alysis_code.run_state import (
    RUN_STATUS_APPROVED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_INTERRUPTED,
    RUN_STATUS_RUNNING,
    compare_plan_fingerprints,
    plan_fingerprint,
)
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult

_WORKSPACE_LOCK_SUBDIR = ".alysis/workspace_execution"

# The child holds the lock and marks the run running, then parks. It is the stand-in
# for a real `forge exec` that has reached the middle of a task -- everything before
# the agent call, which is the part that leaves state on disk.
_HOLDER_SCRIPT = """
import os, socket, sys, time
from pathlib import Path

from alysis_code.forge import load_current_run_paths, load_plan, save_plan, set_current_run_status
from alysis_code.run_state import RUN_STATUS_RUNNING, build_run_owner
from alysis_code.swarm_orchestrator import acquire_swarm_mutation_guard

repo = Path(sys.argv[1])
ready = Path(sys.argv[2])
task_id = sys.argv[3]

paths = load_current_run_paths(repo)
plan = load_plan(paths)
guard = acquire_swarm_mutation_guard(paths, mode="forge_exec:" + task_id, wait=False)
set_current_run_status(
    paths,
    RUN_STATUS_RUNNING,
    reason="forge exec started for " + task_id,
    owner=build_run_owner(pid=os.getpid(), hostname=socket.gethostname(), mode="forge_exec:" + task_id),
    plan=plan,
)
for task in plan["tasks"]:
    if task["id"] == task_id:
        task["status"] = "in_progress"
save_plan(paths, plan)
ready.write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(0.2)
"""


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _two_task_plan(repo: Path) -> tuple[object, dict[str, str]]:
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Build two slices"
    plan["summary"] = "Build two slices"
    files: dict[str, str] = {}
    for title, rel_path in (
        ("Create alpha module", "src/alpha.py"),
        ("Create beta module", "src/beta.py"),
    ):
        task = add_task(
            plan,
            title=title,
            description=f"Task created from planning chat: {title}",
            acceptance_criteria=[f"{rel_path} exists"],
            estimated_files=[rel_path],
        )
        task["write_scope"] = [rel_path]
        files[task["id"]] = rel_path
    save_plan(paths, plan)
    return paths, files


def _passing_verification(monkeypatch, *, command: str = "pytest -q") -> None:
    def fake_run_task_verification(*, artifact_path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification passed\n", encoding="utf-8")
        return VerifyRunResult(
            commands=[command],
            command_results=[
                VerifyCommandResult(
                    command=command,
                    effective_command=command,
                    exit_code=0,
                    output="ok\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_run_task_verification)


def _writing_agent(monkeypatch, files: dict[str, str], *, order: list[str]) -> list[str]:
    """Write each task's file, in the order the sequential runner is expected to pick.

    Position-driven rather than "whichever id appears in the prompt", because a task's
    instruction legitimately mentions its predecessors -- matching on that would make
    the second task rewrite the first task's file and produce no material change.
    """
    calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        assert len(calls) < len(order), "the agent ran more times than there are tasks"
        task_id = order[len(calls)]
        assert task_id in instruction, f"expected {task_id} at position {len(calls)}"
        calls.append(task_id)
        target = root / files[task_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated by {task_id}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    return calls


def _kill_a_running_exec(repo: Path, tmp_path: Path, task_id: str) -> int:
    """Start a real lock-holding execution, then kill -9 it. Returns its pid."""
    ready = tmp_path / "holder_ready"
    env = {**os.environ, **_env(tmp_path)}
    src_dir = os.fspath(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        [src_dir, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, os.fspath(repo), os.fspath(ready), task_id],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if ready.exists():
                break
            if child.poll() is not None:
                _out, err = child.communicate()
                raise AssertionError(f"lock holder died early: {err.decode(errors='replace')}")
            time.sleep(0.05)
        else:  # pragma: no cover - only on a pathologically slow machine
            raise AssertionError("lock holder never signalled readiness")
        pid = int(ready.read_text(encoding="utf-8").strip())
    finally:
        # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup handler runs, so
        # the lock file and the `running` pointer survive exactly as after a crash.
        child.kill()
        child.wait(timeout=30)
    return pid


def _wait_for_pid_to_disappear(pid: int) -> None:
    from alysis_code.run_lock import _process_is_running

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _process_is_running(pid) is False:
            return
        time.sleep(0.05)
    raise AssertionError(f"killed pid {pid} still looks alive")


def _assert_lock_is_recoverable(repo: Path, pid: int) -> None:
    """The precondition every crash-recovery assertion below depends on.

    Checked explicitly so an OS-level pid-reuse race (the killed pid handed to an
    unrelated process before we look) fails here, naming what happened, instead of
    surfacing later as a baffling "resume refused" or "status is still running".
    """
    described = describe_run_mutation_lock(repo / _WORKSPACE_LOCK_SUBDIR)
    assert described is not None, "the killed run left no workspace lock to recover"
    staleness = described["staleness"]
    assert staleness["verdict"] == STALENESS_STALE, (
        f"the lock left by killed pid {pid} was not classified stale: {staleness}"
    )


def _events(output: str) -> list[dict]:
    parsed = [parse_event_line(line) for line in output.splitlines()]
    return [event for event in parsed if event is not None]


def test_killed_exec_becomes_interrupted_and_resume_finishes_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    """kill -9 mid-task, then: status interrupted, no unlock needed, resume completes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    paths, files = _two_task_plan(repo)
    first_task = sorted(files)[0]

    assert current_run_status(repo) == RUN_STATUS_APPROVED

    pid = _kill_a_running_exec(repo, tmp_path, first_task)
    _wait_for_pid_to_disappear(pid)

    # The crash residue is real: pointer still claims running, the task is still
    # in_progress, and both lock files are still on disk.
    raw_pointer = json.loads(current_run_pointer_path(repo).read_text(encoding="utf-8"))
    assert raw_pointer["status"] == RUN_STATUS_RUNNING
    assert raw_pointer["run_owner"]["pid"] == pid
    assert load_plan(paths)["tasks"][0]["status"] == "in_progress"
    workspace_lock_dir = repo / _WORKSPACE_LOCK_SUBDIR
    assert (workspace_lock_dir / "active_execution.lock.json").exists()

    # 1. The dead owner's lock is classified stale on this host, so recovery is
    #    automatic -- `forge unlock` is an inspection tool here, not a prerequisite.
    workspace_lock = describe_run_mutation_lock(workspace_lock_dir)
    assert workspace_lock is not None
    assert workspace_lock["staleness"]["verdict"] == STALENESS_STALE
    assert workspace_lock["staleness"]["kind"] == "dead_pid"

    # 2. Reading the run is what demotes the phantom "active" run to interrupted.
    load_current_run_paths(repo)
    assert current_run_status(repo) == RUN_STATUS_INTERRUPTED

    status_result = CliRunner().invoke(
        alysis_app,
        ["forge", "--machine", "status", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert status_result.exit_code == 0, status_result.output
    terminal = _events(status_result.output)[-1]
    assert terminal["data"]["run_status"] == RUN_STATUS_INTERRUPTED
    assert terminal["data"]["resumable"] is True

    # 3. Resume finishes the run -- without any unlock step in between.
    _passing_verification(monkeypatch)
    expected_order = sorted(files)
    agent_calls = _writing_agent(monkeypatch, files, order=expected_order)

    resume_result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "resume",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "strict",
            "--verify-cmd",
            "pytest -q",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert resume_result.exit_code == 0, resume_result.output
    assert agent_calls == expected_order
    for rel_path in files.values():
        assert (repo / rel_path).exists(), rel_path
    assert [task["status"] for task in load_plan(paths)["tasks"]] == ["done", "done"]
    assert current_run_status(repo) == RUN_STATUS_COMPLETED
    # The recovered lock did not leak into the finished state.
    assert not (workspace_lock_dir / "active_execution.lock.json").exists()


def test_forge_unlock_reports_and_clears_the_dead_owners_lock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _two_task_plan(repo)

    pid = _kill_a_running_exec(repo, tmp_path, "T01")
    _wait_for_pid_to_disappear(pid)
    _assert_lock_is_recoverable(repo, pid)

    result = CliRunner().invoke(
        alysis_app,
        ["forge", "--machine", "unlock", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    data = _events(result.output)[-1]["data"]
    assert data["cleared_count"] == 2  # workspace lock + run lock
    assert data["blocked_count"] == 0
    assert data["run_status"] == RUN_STATUS_INTERRUPTED
    verdicts = [lock["staleness"]["verdict"] for lock in data["locks"] if lock.get("present")]
    assert verdicts == [STALENESS_STALE, STALENESS_STALE]
    assert not (repo / _WORKSPACE_LOCK_SUBDIR / "active_execution.lock.json").exists()


def test_forge_unlock_keeps_a_lock_it_cannot_prove_dead_until_forced(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _two_task_plan(repo)

    lock_dir = repo / _WORKSPACE_LOCK_SUBDIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "active_execution.lock.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "workspace:demo",
                "mode": "forge_run:cli",
                "kind": "lock",
                "pid": 4242,
                "hostname": "some-other-laptop",
                "owner_id": "some-other-laptop:4242",
                "acquired_at": "2026-01-01T00:00:00+00:00",
                "last_heartbeat_at": "2026-01-01T00:00:00+00:00",
                "heartbeat_interval_s": 15.0,
                "heartbeat_ttl_s": 120.0,
                "owner_token": "elsewhere",
                "workspace_root": os.fspath(repo),
                "run_dir": os.fspath(lock_dir),
            }
        ),
        encoding="utf-8",
    )

    kept = CliRunner().invoke(
        alysis_app,
        ["forge", "--machine", "unlock", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert kept.exit_code == 1, kept.output
    kept_data = _events(kept.output)[-1]["data"]
    assert kept_data["blocked_count"] == 1
    assert kept_data["cleared_count"] == 0
    workspace_entry = next(lock for lock in kept_data["locks"] if lock.get("present"))
    assert workspace_entry["staleness"]["verdict"] == STALENESS_AMBIGUOUS
    assert "another host" in workspace_entry["staleness"]["reason"]
    assert (lock_dir / "active_execution.lock.json").exists()

    forced = CliRunner().invoke(
        alysis_app,
        ["forge", "--machine", "unlock", "--path", os.fspath(repo), "--force"],
        env=_env(tmp_path),
    )
    assert forced.exit_code == 0, forced.output
    forced_data = _events(forced.output)[-1]["data"]
    assert forced_data["cleared_count"] == 1
    assert forced_data["forced"] is True
    assert not (lock_dir / "active_execution.lock.json").exists()


def test_expired_heartbeat_recovers_a_lock_whose_pid_was_recycled() -> None:
    """A live pid is not proof of life once the heartbeat contract has lapsed."""
    metadata = {
        "schema_version": 2,
        "pid": os.getpid(),  # stands in for a recycled id: alive, but not the owner
        "hostname": socket.gethostname(),
        "acquired_at": "2026-01-01T00:00:00+00:00",
        "last_heartbeat_at": "2026-01-01T00:00:00+00:00",
        "heartbeat_interval_s": 15.0,
        "heartbeat_ttl_s": 120.0,
    }

    staleness = assess_lock_staleness(metadata)

    assert staleness.verdict == STALENESS_STALE
    assert staleness.kind == "heartbeat_expired"
    assert staleness.process_running is True


def test_the_grace_recheck_saves_a_lock_whose_owner_beats_again(tmp_path: Path) -> None:
    """A heartbeat-expired lock is not taken from an owner that comes back to life.

    The suspend-and-wake case: the lock looks lapsed at the moment it is read, but the
    owner is alive and beats again within one interval. The grace re-read must see the
    refreshed heartbeat and hand the lock back rather than recovering it.
    """
    import threading

    from alysis_code.run_lock import RunMutationConflictError, acquire_run_mutation_guard

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    lock_path = run_dir / "active_execution.lock.json"
    lapsed = {
        "schema_version": 2,
        "run_id": "run-1",
        "mode": "forge_run:cli",
        "kind": "lock",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": "2026-01-01T00:00:00+00:00",
        "last_heartbeat_at": "2026-01-01T00:00:00+00:00",
        "heartbeat_interval_s": 1.0,
        "heartbeat_ttl_s": 2.0,
        "owner_token": "sleeping-owner",
        "workspace_root": os.fspath(tmp_path),
        "run_dir": os.fspath(run_dir),
    }
    lock_path.write_text(json.dumps(lapsed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert assess_lock_staleness(lapsed).verdict == STALENESS_STALE

    def beat_again() -> None:
        time.sleep(0.3)
        lock_path.write_text(
            json.dumps(
                {**lapsed, "last_heartbeat_at": "2099-01-01T00:00:00+00:00"},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    beater = threading.Thread(target=beat_again, daemon=True)
    beater.start()
    try:
        with pytest.raises(RunMutationConflictError) as excinfo:
            acquire_run_mutation_guard(
                run_id="run-1",
                mode="forge_run:second",
                run_dir=run_dir,
                workspace_root=tmp_path,
            )
    finally:
        beater.join(timeout=5)

    assert "changed while recovery was in progress" in str(excinfo.value)
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner_token"] == "sleeping-owner"


def test_a_failed_heartbeat_write_does_not_stop_the_heartbeat() -> None:
    """One failed refresh must not end the loop -- that would reap a live owner.

    On Windows an ``os.replace`` onto the lock file races any concurrent reader and
    raises ``PermissionError``. If that killed the thread, the owner would stop
    beating, its own TTL would expire, and the next acquirer would take the lock
    out from under a process that is still mutating the workspace.
    """
    from alysis_code.run_lock import _HeartbeatWorker

    attempts: list[int] = []

    def flaky_refresh() -> None:
        attempts.append(len(attempts))
        if len(attempts) <= 2:
            raise PermissionError("the lock file was open for reading")

    worker = _HeartbeatWorker(refresh=flaky_refresh, interval_s=0.02)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(attempts) < 5:
            time.sleep(0.02)
    finally:
        worker.stop()

    assert len(attempts) >= 5, "the worker gave up after a failed refresh"
    assert worker.consecutive_failures == 0


def test_the_guard_keeps_its_own_heartbeat_fresh(tmp_path: Path, monkeypatch) -> None:
    from alysis_code.run_lock import acquire_run_mutation_guard, inspect_run_mutation_lock

    monkeypatch.setenv("ALYSIS_RUN_LOCK_HEARTBEAT_INTERVAL_S", "0.1")
    run_dir = tmp_path / "run"

    with acquire_run_mutation_guard(
        run_id="run-1",
        mode="forge_run:cli",
        run_dir=run_dir,
        workspace_root=tmp_path,
    ):
        first = inspect_run_mutation_lock(run_dir)
        assert first is not None
        assert first["heartbeat_interval_s"] == 0.1
        # Floored at twice the interval so a healthy owner is never reaped between beats.
        assert first["heartbeat_ttl_s"] >= 0.2

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            later = inspect_run_mutation_lock(run_dir)
            if later is not None and later["last_heartbeat_at"] != first["last_heartbeat_at"]:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - the heartbeat thread never ran
            raise AssertionError("the guard never refreshed its own heartbeat")


def test_a_lock_without_a_heartbeat_contract_is_never_reaped_by_age() -> None:
    """Locks from older builds never promised to beat, so their silence proves nothing."""
    metadata = {
        "schema_version": 1,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": "2020-01-01T00:00:00+00:00",
    }

    staleness = assess_lock_staleness(metadata)

    assert staleness.verdict == STALENESS_ACTIVE
    assert staleness.age_s is not None and staleness.age_s > 0


def test_conflict_message_carries_age_owner_host_and_the_recovery_command(
    tmp_path: Path,
) -> None:
    from alysis_code.run_lock import RunMutationConflictError, acquire_run_mutation_guard

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "active_execution.lock.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "run-1",
                "mode": "forge_swarm",
                "kind": "lock",
                "pid": 4242,
                "hostname": "other-host",
                "owner_id": "other-host:4242:abc",
                "acquired_at": "2026-01-01T00:00:00+00:00",
                "last_heartbeat_at": "2026-01-01T00:00:00+00:00",
                "owner_token": "elsewhere",
                "workspace_root": os.fspath(tmp_path),
                "run_dir": os.fspath(run_dir),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RunMutationConflictError) as excinfo:
        acquire_run_mutation_guard(
            run_id="run-1",
            mode="forge_run:cli",
            run_dir=run_dir,
            workspace_root=tmp_path,
        )

    message = str(excinfo.value)
    assert "owner=other-host:4242:abc" in message
    assert "host=other-host" in message
    assert "age=" in message
    assert "heartbeat_age=" in message
    assert "alysis forge unlock --path" in message


def test_resume_refuses_a_drifted_plan_until_reapproved(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    paths, files = _two_task_plan(repo)
    first_task = sorted(files)[0]

    pid = _kill_a_running_exec(repo, tmp_path, first_task)
    _wait_for_pid_to_disappear(pid)
    _assert_lock_is_recoverable(repo, pid)
    load_current_run_paths(repo)
    assert current_run_status(repo) == RUN_STATUS_INTERRUPTED

    # An edit made while nothing was running: exactly the case resume must catch.
    plan = load_plan(paths)
    plan["tasks"][1]["acceptance_criteria"] = ["src/beta.py exists and exports build()"]
    plan["project_goal"] = "Build two slices, differently"
    save_plan(paths, plan)

    blocked = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "resume",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert blocked.exit_code == 1, blocked.output
    terminal = _events(blocked.output)[-1]
    assert terminal["event"] == "error"
    drift = terminal["data"]["drift"]
    assert drift["changed"] is True
    assert drift["goal_changed"] is True
    second_task = sorted(files)[1]
    assert [entry["task_id"] for entry in drift["tasks_changed"]] == [second_task]
    assert drift["tasks_changed"][0]["fields"] == ["acceptance_criteria"]
    # Refusing to resume must not throw the approval away.
    assert current_run_status(repo) == RUN_STATUS_INTERRUPTED

    _passing_verification(monkeypatch)
    _writing_agent(monkeypatch, files, order=sorted(files))

    accepted = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "resume",
            "--path",
            os.fspath(repo),
            "--reapprove",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "strict",
            "--verify-cmd",
            "pytest -q",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert accepted.exit_code == 0, accepted.output
    assert current_run_status(repo) == RUN_STATUS_COMPLETED


def test_resume_dry_run_reports_without_touching_anything(tmp_path: Path) -> None:
    """`--dry-run` must not re-arm tasks, re-approve, or move the run status."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    paths, files = _two_task_plan(repo)
    first_task = sorted(files)[0]

    pid = _kill_a_running_exec(repo, tmp_path, first_task)
    _wait_for_pid_to_disappear(pid)
    _assert_lock_is_recoverable(repo, pid)
    load_current_run_paths(repo)
    assert current_run_status(repo) == RUN_STATUS_INTERRUPTED

    plan_before = json.loads(paths.plan_json_path.read_text(encoding="utf-8"))
    pointer_before = json.loads(current_run_pointer_path(repo).read_text(encoding="utf-8"))

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "resume",
            "--path",
            os.fspath(repo),
            "--dry-run",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    data = _events(result.output)[-1]["data"]
    assert data["dry_run"] is True
    assert data["resumed"] is False
    assert data["remaining"] == sorted(files)

    assert json.loads(paths.plan_json_path.read_text(encoding="utf-8")) == plan_before
    assert json.loads(current_run_pointer_path(repo).read_text(encoding="utf-8")) == pointer_before
    assert plan_before["tasks"][0]["status"] == "in_progress"


def test_resume_declines_runs_that_were_never_interrupted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _two_task_plan(repo)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "resume",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2, result.output
    terminal = _events(result.output)[-1]
    assert terminal["event"] == "error"
    assert terminal["data"]["run_status"] == RUN_STATUS_APPROVED
    assert "forge run" in terminal["data"]["message"]


def test_pointer_status_tracks_the_plan_across_the_readiness_gate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    assert current_run_status(repo) == RUN_STATUS_DRAFT

    plan = load_plan(paths)
    plan["project_goal"] = "Ship the parser"
    plan["summary"] = "Ship the parser"
    add_task(
        plan,
        title="Implement the parser",
        description="Write src/parser.py",
        acceptance_criteria=["src/parser.py exists"],
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)
    assert current_run_status(repo) == RUN_STATUS_APPROVED

    pointer = json.loads(current_run_pointer_path(repo).read_text(encoding="utf-8"))
    assert pointer["schema_version"] == 5
    assert (
        compare_plan_fingerprints(pointer["plan_fingerprint"], plan_fingerprint(plan)).changed
        is False
    )
    assert [entry["status"] for entry in pointer["status_history"]] == [
        RUN_STATUS_DRAFT,
        RUN_STATUS_APPROVED,
    ]


def test_legacy_pointer_without_a_status_reads_as_draft(tmp_path: Path) -> None:
    """Pointers written before the lifecycle block must still load, claiming nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    pointer_path = current_run_pointer_path(repo)
    legacy = {
        key: value
        for key, value in json.loads(pointer_path.read_text(encoding="utf-8")).items()
        if key not in {"status", "status_updated_at", "status_history", "plan_fingerprint"}
    }
    legacy["schema_version"] = 4
    pointer_path.write_text(json.dumps(legacy), encoding="utf-8")

    assert current_run_status(repo) == RUN_STATUS_DRAFT
    assert load_current_run_paths(repo).run_id == paths.run_id
