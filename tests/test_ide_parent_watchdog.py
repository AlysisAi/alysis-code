from __future__ import annotations

import os
import threading

from alysis_code.ide.parent_watchdog import (
    PARENT_PID_ENV,
    parent_pid_from_env,
    process_is_alive,
    start_parent_watchdog,
)


def test_parent_pid_parsing_rejects_junk_zero_and_self() -> None:
    assert parent_pid_from_env({}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: ""}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: "abc"}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: "-5"}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: "0"}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: str(os.getpid())}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: " 4242 "}) == 4242


def test_watchdog_is_inert_without_the_variable() -> None:
    assert start_parent_watchdog(grace_seconds=0, environ={}) is None


def test_watchdog_closes_then_force_exits_once_the_parent_is_gone() -> None:
    alive = {"value": True}
    events: list[str] = []
    exited = threading.Event()

    def exit_process(code: int) -> None:
        events.append(f"exit:{code}")
        exited.set()

    thread = start_parent_watchdog(
        grace_seconds=0.01,
        environ={PARENT_PID_ENV: "4242"},
        is_alive=lambda pid: alive["value"],
        poll_seconds=0.01,
        on_parent_exit=lambda pid: events.append(f"close:{pid}"),
        exit_process=exit_process,
    )
    assert thread is not None and thread.daemon
    assert not exited.wait(0.1), "must not exit while the parent is alive"
    assert events == []

    alive["value"] = False
    assert exited.wait(2.0), "must exit after the parent dies"
    assert events == ["close:4242", "exit:1"]


def test_watchdog_survives_a_failing_close_hook() -> None:
    exited = threading.Event()

    def boom(pid: int) -> None:
        raise RuntimeError("close failed")

    start_parent_watchdog(
        grace_seconds=0,
        environ={PARENT_PID_ENV: "4242"},
        is_alive=lambda pid: False,
        poll_seconds=0.01,
        on_parent_exit=boom,
        exit_process=lambda code: exited.set(),
    )
    assert exited.wait(2.0)


def test_a_detection_error_never_kills_a_healthy_bridge() -> None:
    exited = threading.Event()

    def broken(pid: int) -> bool:
        raise OSError("cannot query")

    thread = start_parent_watchdog(
        grace_seconds=0,
        environ={PARENT_PID_ENV: "4242"},
        is_alive=broken,
        poll_seconds=0.01,
        exit_process=lambda code: exited.set(),
    )
    assert thread is not None
    thread.join(2.0)
    assert not thread.is_alive()
    assert not exited.is_set()


def test_process_is_alive_for_self_and_for_a_dead_child() -> None:
    assert process_is_alive(os.getpid()) is True
    if os.name != "nt":
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os._exit(0)
        os.waitpid(pid, 0)
        assert process_is_alive(pid) is False
