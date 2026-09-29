from __future__ import annotations

import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import psutil
import pytest

from alysis_code.agent import checkpoint_worker as worker


def test_inspection_and_kill_denial_report_incomplete_cleanup(monkeypatch):
    process = Mock(pid=101)
    process.poll.return_value = None
    process.kill.side_effect = PermissionError("kill denied")
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 0)
    monkeypatch.setattr(worker.psutil, "Process", Mock(side_effect=psutil.AccessDenied(101)))
    assert worker._kill_owned_worker(process, stop_at=time.monotonic()) is False
    process.kill.assert_called_once()
    assert process.wait.call_args.kwargs["timeout"] == 0.0


@pytest.mark.parametrize("failure", [psutil.AccessDenied(102), OSError("signal unavailable")])
def test_child_signal_failure_still_kills_leader_and_checks_pending_children(monkeypatch, failure):
    process = Mock(pid=101)
    process.poll.return_value = None
    child = Mock(pid=102)
    child.kill.side_effect = failure
    leader = Mock()
    leader.children.return_value = [child]
    monkeypatch.setattr(worker.psutil, "Process", lambda _pid: leader)
    monkeypatch.setattr(worker.psutil, "wait_procs", lambda _children, timeout: ([], [child]))
    assert worker._kill_owned_worker(process, stop_at=time.monotonic()) is False
    process.kill.assert_called_once()
    child.kill.assert_called_once()


def test_process_exiting_during_kill_is_confirmed_by_final_wait(monkeypatch):
    process = Mock(pid=101)
    process.poll.return_value = None
    process.kill.side_effect = ProcessLookupError()
    child = Mock(pid=102)
    child.kill.side_effect = psutil.NoSuchProcess(102)
    leader = Mock()
    leader.children.return_value = [child]
    monkeypatch.setattr(worker.psutil, "Process", lambda _pid: leader)
    monkeypatch.setattr(worker.psutil, "wait_procs", lambda children, timeout: (children, []))
    assert worker._kill_owned_worker(process, stop_at=time.monotonic()) is True


def test_child_wait_inspection_error_remains_unverified(monkeypatch):
    process = Mock(pid=101)
    process.poll.return_value = None
    leader = Mock()
    leader.children.return_value = [Mock(pid=102)]
    monkeypatch.setattr(worker.psutil, "Process", lambda _pid: leader)
    monkeypatch.setattr(worker.psutil, "wait_procs", Mock(side_effect=psutil.AccessDenied(102)))
    assert worker._kill_owned_worker(process, stop_at=time.monotonic()) is False


def test_exited_launcher_does_not_prove_inherited_pipe_descendants_are_gone(monkeypatch):
    process = Mock(pid=101)
    process.poll.return_value = 0
    monkeypatch.setattr(worker.psutil, "Process", Mock(side_effect=AssertionError("PID reused")))
    assert worker._kill_owned_worker(process, stop_at=time.monotonic()) is False
    process.kill.assert_not_called()


@pytest.mark.parametrize("timeout_failure", [True, False])
def test_incomplete_communication_never_closes_blocked_pipes_on_caller(
    monkeypatch, timeout_failure
):
    closing = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class BlockedPipe:
        def close(self):
            closing.set()
            release.wait(3)
            closed.set()

    process = Mock(pid=101)
    process.stdin = None
    process.stdout = BlockedPipe()
    process.stderr = None
    error = (
        subprocess.TimeoutExpired("worker", 0) if timeout_failure else OSError("pipe read failed")
    )
    process.communicate.side_effect = error
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(worker, "_kill_owned_worker", lambda *_args, **_kwargs: False)
    expected = worker.CheckpointWorkerExpired if timeout_failure else OSError
    try:
        started = time.monotonic()
        with pytest.raises(expected) as caught:
            worker.run_checkpoint_worker({}, timeout=0.1)
        assert time.monotonic() - started < 0.3
        assert caught.value.cleanup_pending is True
        assert closing.wait(1)
        assert not closed.is_set()
    finally:
        release.set()
        assert closed.wait(1)


def test_real_worker_kill_denial_returns_within_budget_and_reports_pending(tmp_path, monkeypatch):
    marker = tmp_path / "worker-started"
    command = [
        getattr(sys, "_base_executable", sys.executable),
        "-I",
        "-c",
        f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('ready'); time.sleep(10)",
    ]
    original_popen = subprocess.Popen
    processes = []

    def denied_kill():
        raise PermissionError("injected worker kill denial")

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        process.kill = denied_kill
        return process

    monkeypatch.setattr(worker, "worker_command", lambda: command)
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    try:
        started = time.monotonic()
        with pytest.raises(worker.CheckpointWorkerExpired) as caught:
            worker.run_checkpoint_worker({}, timeout=0.7)
        assert time.monotonic() - started < 1.2
        assert marker.exists(), "the real worker must reach its blocking operation"
        assert caught.value.cleanup_pending is True
        assert len(processes) == 1
        assert processes[0].poll() is None
    finally:
        for process in processes:
            # Release only the owned worker created by this test, using the
            # original Popen implementation after observing denied cleanup.
            if process.poll() is None:
                original_popen.kill(process)
            process.wait(timeout=2)
