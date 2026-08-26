"""Server job status comes from the worker's terminal event, not just its exit code."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.forge_events import (
    EVENT_ERROR,
    EVENT_RUN_COMPLETED,
    EVENT_TASK_FAILED,
    SCHEMA_VERSION,
)
from alysis_code.server.settings import ServerSettings
from alysis_code.server.store import ServerStore
from alysis_code.server.worker_runner import (
    STATUS_SOURCE_EXIT_CODE,
    STATUS_SOURCE_TERMINAL_EVENT,
    JobRunner,
    JobState,
    job_status_from_terminal_event,
)


def _settings(tmp_path: Path, *, worker_machine_events: bool = True) -> ServerSettings:
    return ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data",
        token=None,
        max_upload_bytes=2 * 1024 * 1024,
        max_concurrent_jobs=1,
        worker_backend="bwrap",
        worker_sandbox_mode="strict",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_base_url=False,
        allow_client_model=True,
        worker_machine_events=worker_machine_events,
    )


def _event_line(event: str, data: dict[str, object], *, run_id: str = "001-test") -> str:
    payload = {
        "v": SCHEMA_VERSION,
        "event": event,
        "ts": "2026-01-01T00:00:00+00:00",
        "run_id": run_id,
        "data": data,
    }
    return json.dumps(payload) + "\n"


class _ScriptedPopen:
    def __init__(self, lines: list[str], *, exit_code: int) -> None:
        self.stdout = iter(lines)
        self._exit_code = exit_code

    def wait(self) -> int:
        return self._exit_code

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None


class _ScriptedRunner:
    def __init__(self, proc: _ScriptedPopen) -> None:
        self._proc = proc

    def spawn(self, **_kwargs: object) -> _ScriptedPopen:
        return self._proc


def _run_scripted_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lines: list[str],
    exit_code: int,
) -> tuple[JobState, dict[str, object]]:
    settings = _settings(tmp_path)
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    job_paths = store.create_job_paths(run_id, "job_test")
    state = JobState(
        job_id="job_test",
        run_id=run_id,
        status="queued",
        command=["alysis", "forge", "exec", "T01", "--machine"],
        created_at="2026-01-01T00:00:00+00:00",
        logs_path=os.fspath(job_paths.logs_path),
    )
    runner = JobRunner(settings, store)
    monkeypatch.setattr(
        runner,
        "_outer_runner",
        lambda: _ScriptedRunner(_ScriptedPopen(lines, exit_code=exit_code)),
    )
    try:
        runner._run_job_inner(state, run_paths, job_paths)
    finally:
        runner.close()
    result = json.loads(job_paths.result_path.read_text(encoding="utf-8"))
    return state, result


def test_terminal_event_decides_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, result = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=[_event_line(EVENT_RUN_COMPLETED, {"ok": True, "exit_code": 0})],
        exit_code=0,
    )

    assert state.status == "succeeded"
    assert state.status_source == STATUS_SOURCE_TERMINAL_EVENT
    assert result["status"] == "succeeded"
    assert result["status_source"] == STATUS_SOURCE_TERMINAL_EVENT
    assert result["terminal_event"]["event"] == EVENT_RUN_COMPLETED


def test_terminal_event_reporting_failure_wins_over_a_zero_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the job says what happened, the exit code only guesses."""
    state, result = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=[_event_line(EVENT_RUN_COMPLETED, {"ok": False, "exit_code": 1})],
        exit_code=0,
    )

    assert state.status == "failed"
    assert state.exit_code == 0
    assert state.status_source == STATUS_SOURCE_TERMINAL_EVENT
    assert result["status"] == "failed"


def test_error_event_marks_the_job_failed_and_keeps_its_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, result = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=[
            _event_line(EVENT_ERROR, {"kind": "forge_error", "message": "run not found"}),
        ],
        exit_code=2,
    )

    assert state.status == "failed"
    assert state.status_source == STATUS_SOURCE_TERMINAL_EVENT
    assert state.error == "run not found"
    assert result["error"] == "run not found"


def test_exit_code_is_the_fallback_when_no_terminal_event_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, result = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=["Plan saved: /workspace/.alysis/plan/PLAN.md\n", "done\n"],
        exit_code=0,
    )

    assert state.status == "succeeded"
    assert state.status_source == STATUS_SOURCE_EXIT_CODE
    assert result["status_source"] == STATUS_SOURCE_EXIT_CODE
    assert result["terminal_event"] is None


def test_nonzero_exit_without_events_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=["boom\n"],
        exit_code=3,
    )

    assert state.status == "failed"
    assert state.status_source == STATUS_SOURCE_EXIT_CODE


def test_non_terminal_events_do_not_decide_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=[_event_line(EVENT_TASK_FAILED, {"task_id": "T01"})],
        exit_code=0,
    )

    assert state.status == "succeeded"
    assert state.status_source == STATUS_SOURCE_EXIT_CODE


def test_last_terminal_event_wins_and_worker_prose_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = _run_scripted_job(
        tmp_path,
        monkeypatch,
        lines=[
            "worker chatter that is not an event\n",
            "{not json at all}\n",
            _event_line(EVENT_RUN_COMPLETED, {"ok": False, "exit_code": 1}),
        ],
        exit_code=0,
    )

    assert state.status == "failed"
    assert state.status_source == STATUS_SOURCE_TERMINAL_EVENT


def test_job_logs_still_contain_every_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = ServerStore(settings)
    run_id = store.create_empty_run()
    run_paths = store.get_run_paths(run_id)
    job_paths = store.create_job_paths(run_id, "job_logs")
    state = JobState(
        job_id="job_logs",
        run_id=run_id,
        status="queued",
        command=["alysis", "forge", "exec", "T01", "--machine"],
        created_at="2026-01-01T00:00:00+00:00",
        logs_path=os.fspath(job_paths.logs_path),
    )
    runner = JobRunner(settings, store)
    monkeypatch.setattr(
        runner,
        "_outer_runner",
        lambda: _ScriptedRunner(
            _ScriptedPopen(
                [
                    "progress line\n",
                    _event_line(EVENT_RUN_COMPLETED, {"ok": True, "exit_code": 0}),
                ],
                exit_code=0,
            )
        ),
    )
    try:
        runner._run_job_inner(state, run_paths, job_paths)
    finally:
        runner.close()
    logs = job_paths.logs_path.read_text(encoding="utf-8")

    assert "progress line" in logs
    assert EVENT_RUN_COMPLETED in logs


@pytest.mark.parametrize(
    ("event", "data", "expected"),
    [
        (EVENT_RUN_COMPLETED, {"ok": True}, "succeeded"),
        (EVENT_RUN_COMPLETED, {"ok": False}, "failed"),
        (EVENT_RUN_COMPLETED, {}, None),
        (EVENT_ERROR, {"message": "x"}, "failed"),
        (EVENT_TASK_FAILED, {"task_id": "T01"}, None),
    ],
)
def test_job_status_from_terminal_event_mapping(
    event: str,
    data: dict[str, object],
    expected: str | None,
) -> None:
    payload = {"v": SCHEMA_VERSION, "event": event, "ts": "t", "run_id": None, "data": data}
    assert job_status_from_terminal_event(payload) == expected


def test_job_status_from_terminal_event_ignores_nothing() -> None:
    assert job_status_from_terminal_event(None) is None
