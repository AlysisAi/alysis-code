from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.process_reaping import (
    SIGNAL_KILL,
    SIGNAL_NONE,
    SIGNAL_TERM,
    ProcessGroupRegistry,
    ProcessReapOutcome,
    ReapAction,
    ReapEvent,
    TrackedProcessGroup,
    _pgid_is_signalable,
    _process_reaping_enabled,
    process_group_exists,
    process_groups_supported,
    reap_process_group,
    reap_tracked_groups,
    resolve_reap_decision,
    run_in_tracked_process_group,
    survivor_payloads,
)
from alysis_code.runtime_kind import RuntimeKind

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")


# ---------------------------------------------------------------------------
# Test 1: the reaping decision table (runtime kind x event) -- pure
# ---------------------------------------------------------------------------

_AUTONOMOUS_KINDS = (
    RuntimeKind.ONE_SHOT,
    RuntimeKind.FORGE_EXEC,
    RuntimeKind.SWARM_WORKER,
    RuntimeKind.SUBAGENT,
    RuntimeKind.CONFLICT_AUTO_RESOLVE,
)


@pytest.mark.parametrize("kind", _AUTONOMOUS_KINDS)
def test_autonomous_turn_finalization_reaps(kind: RuntimeKind) -> None:
    decision = resolve_reap_decision(
        runtime_kind=kind, event=ReapEvent.TURN_FINALIZATION, enabled=True
    )
    assert decision.action is ReapAction.REAP
    assert decision.reason == "autonomous_turn_finalization"


def test_interactive_turn_finalization_reports_without_killing() -> None:
    decision = resolve_reap_decision(
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        event=ReapEvent.TURN_FINALIZATION,
        enabled=True,
    )
    assert decision.action is ReapAction.REPORT
    assert decision.reason == "interactive_turn_keeps_user_processes"


@pytest.mark.parametrize("kind", (RuntimeKind.INTERACTIVE_CHAT, *_AUTONOMOUS_KINDS))
def test_session_close_always_reaps(kind: RuntimeKind) -> None:
    decision = resolve_reap_decision(runtime_kind=kind, event=ReapEvent.SESSION_CLOSE, enabled=True)
    assert decision.action is ReapAction.REAP
    assert decision.reason == "session_close"


@pytest.mark.parametrize("event", tuple(ReapEvent))
@pytest.mark.parametrize("kind", (RuntimeKind.INTERACTIVE_CHAT, RuntimeKind.ONE_SHOT))
def test_kill_switch_reverts_to_legacy(kind: RuntimeKind, event: ReapEvent) -> None:
    decision = resolve_reap_decision(runtime_kind=kind, event=event, enabled=False)
    assert decision.action is ReapAction.SKIP
    assert decision.reason == "process_reaping_disabled"


def test_unknown_runtime_kind_is_treated_as_interactive() -> None:
    # An unrecognized kind must never escalate to killing.
    decision = resolve_reap_decision(
        runtime_kind="something_new", event=ReapEvent.TURN_FINALIZATION, enabled=True
    )
    assert decision.action is ReapAction.REPORT


# ---------------------------------------------------------------------------
# Test 2: kill scope -- only runner-created pgids are ever signalable
# ---------------------------------------------------------------------------


@posix_only
def test_own_process_group_is_never_signalable() -> None:
    assert _pgid_is_signalable(os.getpgrp()) is False


@pytest.mark.parametrize("candidate", [0, 1, -1, -5, None, "1234", 1.5])
def test_non_positive_and_non_integer_pgids_are_refused(candidate: object) -> None:
    assert _pgid_is_signalable(candidate) is False


@posix_only
def test_registry_refuses_to_record_own_group() -> None:
    registry = ProcessGroupRegistry()
    assert registry.register(pgid=os.getpgrp(), command="x", origin="test") is None
    assert registry.tracked() == ()


@posix_only
def test_untracked_process_is_left_alone() -> None:
    # A decoy started outside the runner: reaping must not touch it. This is the
    # safety property -- reaping is scoped by what was recorded, not by search.
    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        registry = ProcessGroupRegistry()
        # Registry is empty: nothing was recorded, so nothing may be signalled.
        assert reap_tracked_groups(registry) == ()
        time.sleep(0.2)
        assert decoy.poll() is None, "an untracked process was killed"
    finally:
        decoy.kill()
        decoy.wait(timeout=5)


@posix_only
def test_reaping_only_touches_the_registered_group() -> None:
    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    tracked_child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        registry = ProcessGroupRegistry()
        record = registry.register(
            pgid=os.getpgid(tracked_child.pid), command="sleep 30", origin="test"
        )
        assert record is not None

        outcomes = reap_tracked_groups(registry, grace_seconds=2.0)

        assert len(outcomes) == 1
        assert outcomes[0].signal_used in {SIGNAL_TERM, SIGNAL_KILL}
        tracked_child.wait(timeout=10)
        time.sleep(0.2)
        assert decoy.poll() is None, "reaping escaped its tracked group"
    finally:
        for process in (decoy, tracked_child):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


# ---------------------------------------------------------------------------
# Test 3: spawn -> track -> finalize (integration, real short-lived processes)
# ---------------------------------------------------------------------------


@posix_only
def test_completed_command_leaves_nothing_tracked(tmp_path: Path) -> None:
    registry = ProcessGroupRegistry()
    completed = run_in_tracked_process_group(
        "echo hello",
        shell=True,
        cwd=str(tmp_path),
        timeout=30,
        registry=registry,
        origin="test",
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "hello"
    assert registry.live() == ()


@posix_only
def test_timed_out_command_stays_tracked_then_is_reaped(tmp_path: Path) -> None:
    # The sklearn-14710 shape: the shell is killed by the timeout, its child
    # survives. The group must remain tracked so finalization can reap it.
    registry = ProcessGroupRegistry()
    script = f"{sys.executable} -c 'import time; time.sleep(60)' & wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_tracked_process_group(
            script,
            shell=True,
            cwd=str(tmp_path),
            timeout=1,
            registry=registry,
            origin="test",
        )

    live = registry.live()
    assert len(live) == 1, "the orphaned worker's group was not tracked"
    pgid = live[0].pgid

    outcomes = reap_tracked_groups(registry, grace_seconds=3.0)
    assert len(outcomes) == 1
    assert outcomes[0].signal_used in {SIGNAL_TERM, SIGNAL_KILL}
    assert outcomes[0].pgid == pgid
    assert not process_group_exists(pgid)
    assert registry.live() == ()


@posix_only
def test_backgrounded_command_stays_tracked_after_the_call_returns(tmp_path: Path) -> None:
    registry = ProcessGroupRegistry()
    script = f"nohup {sys.executable} -c 'import time; time.sleep(60)' >/dev/null 2>&1 &"
    completed = run_in_tracked_process_group(
        script,
        shell=True,
        cwd=str(tmp_path),
        timeout=30,
        registry=registry,
        origin="test",
    )
    assert completed.returncode == 0
    try:
        live = registry.live()
        assert len(live) == 1, "a backgrounded job was not tracked"
        assert survivor_payloads(registry)[0]["pgid"] == live[0].pgid
    finally:
        reap_tracked_groups(registry, grace_seconds=3.0)
    assert registry.live() == ()


# ---------------------------------------------------------------------------
# Test 4: escalation order (SIGTERM, bounded grace, SIGKILL)
# ---------------------------------------------------------------------------


@posix_only
def test_graceful_group_exits_on_sigterm(tmp_path: Path) -> None:
    registry = ProcessGroupRegistry()
    script = f"nohup {sys.executable} -c 'import time; time.sleep(60)' >/dev/null 2>&1 &"
    run_in_tracked_process_group(
        script, shell=True, cwd=str(tmp_path), timeout=30, registry=registry, origin="test"
    )
    live = registry.live()
    assert len(live) == 1
    outcome = reap_process_group(live[0], grace_seconds=5.0)
    assert outcome.signal_used == SIGNAL_TERM
    assert not process_group_exists(live[0].pgid)


@posix_only
def test_sigterm_ignoring_group_escalates_to_sigkill(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        # Announce readiness only after the handler is installed, so the test
        # cannot race the interpreter's startup and see a plain SIGTERM death.
        f"pathlib.Path({str(ready)!r}).write_text('1')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    registry = ProcessGroupRegistry()
    script = f"nohup {sys.executable} {stubborn} >/dev/null 2>&1 &"
    run_in_tracked_process_group(
        script, shell=True, cwd=str(tmp_path), timeout=30, registry=registry, origin="test"
    )
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "the stubborn child never installed its SIGTERM handler"

    live = registry.live()
    assert len(live) == 1
    outcome = reap_process_group(live[0], grace_seconds=1.0)
    assert outcome.signal_used == SIGNAL_KILL
    assert not process_group_exists(live[0].pgid)


@posix_only
def test_interrupting_a_tracked_command_kills_its_whole_group(tmp_path: Path) -> None:
    # Before this feature the terminal's SIGINT reached the child directly. Now the
    # command leads its own session, so an interrupt must take the group down or a
    # Ctrl-C would silently leave the test run going.
    registry = ProcessGroupRegistry()
    observed: dict[str, int] = {}

    def _interrupting_factory(args: object, **kwargs: object):
        process = subprocess.Popen(args, **kwargs)  # type: ignore[arg-type]
        observed["pgid"] = os.getpgid(process.pid)

        def _interrupt(timeout: float | None = None):
            time.sleep(0.5)  # let the shell start its background worker
            raise KeyboardInterrupt

        process.communicate = _interrupt  # type: ignore[method-assign]
        return process

    script = f"nohup {sys.executable} -c 'import time; time.sleep(60)' >/dev/null 2>&1 & sleep 30"
    with pytest.raises(KeyboardInterrupt):
        run_in_tracked_process_group(
            script,
            shell=True,
            cwd=str(tmp_path),
            timeout=60,
            registry=registry,
            origin="test",
            popen_factory=_interrupting_factory,
        )

    assert not process_group_exists(observed["pgid"])
    assert registry.live() == ()


def test_untracked_calls_go_straight_to_subprocess_run(monkeypatch, tmp_path: Path) -> None:
    # Without a registry there is nothing to track, so the call must stay the plain
    # subprocess.run it always was -- several callers and their tests patch it.
    seen: list[object] = []

    def _fake_run(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="patched", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    completed = run_in_tracked_process_group(
        "echo hello", shell=True, cwd=str(tmp_path), timeout=30, registry=None
    )
    assert seen == ["echo hello"]
    assert completed.stdout == "patched"


def test_reaping_an_already_dead_group_signals_nothing() -> None:
    record = TrackedProcessGroup(
        pgid=2_147_400_000, command="gone", origin="test", started_at=time.monotonic()
    )
    outcome = reap_process_group(record, grace_seconds=0.1)
    assert outcome.signal_used == SIGNAL_NONE


# ---------------------------------------------------------------------------
# Test 5: Windows no-op path
# ---------------------------------------------------------------------------


def test_windows_host_tracks_nothing_and_signals_nothing(monkeypatch) -> None:
    # Simulate a Windows host: process groups are unsupported, so registration
    # is refused outright and reaping is a no-op rather than a wrong kill.
    monkeypatch.setattr("alysis_code.process_reaping.process_groups_supported", lambda: False)
    registry = ProcessGroupRegistry()
    assert registry.register(pgid=999_999, command="pytest", origin="test") is None
    assert registry.tracked() == ()
    assert reap_tracked_groups(registry) == ()
    assert survivor_payloads(registry) == ()
    assert process_group_exists(999_999) is False


def test_process_groups_supported_matches_platform() -> None:
    assert process_groups_supported() is (os.name != "nt")


def test_run_in_tracked_process_group_works_without_a_registry(tmp_path: Path) -> None:
    # The Windows/no-registry path must still run the command normally.
    completed = run_in_tracked_process_group(
        [sys.executable, "-c", "print('ok')"],
        cwd=str(tmp_path),
        timeout=30,
        registry=None,
        origin="test",
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Test 6: payloads and kill-switch plumbing
# ---------------------------------------------------------------------------


def test_process_reaped_payload_shape() -> None:
    outcome = ProcessReapOutcome(
        pgid=4242,
        command="python -m pytest sklearn/tests",
        origin="shell_run:host",
        runtime_s=91.4,
        signal_used=SIGNAL_TERM,
    )
    assert outcome.payload() == {
        "pgid": 4242,
        "command": "python -m pytest sklearn/tests",
        "origin": "shell_run:host",
        "runtime_s": 91.4,
        "signal_used": "SIGTERM",
    }


def test_survivor_payload_shape() -> None:
    record = TrackedProcessGroup(
        pgid=77, command="npm run dev", origin="shell_run:host", started_at=time.monotonic() - 5
    )
    payload = record.payload()
    assert payload["pgid"] == 77
    assert payload["command"] == "npm run dev"
    assert payload["origin"] == "shell_run:host"
    assert payload["runtime_s"] >= 5.0


def test_kill_switch_env_and_config(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_PROCESS_REAPING", raising=False)
    assert _process_reaping_enabled(AppConfig(model="x", process_reaping_enabled=True)) is True
    assert _process_reaping_enabled(AppConfig(model="x", process_reaping_enabled=False)) is False
    monkeypatch.setenv("ALYSIS_PROCESS_REAPING", "off")
    assert _process_reaping_enabled(AppConfig(model="x", process_reaping_enabled=True)) is False
    monkeypatch.setenv("ALYSIS_PROCESS_REAPING", "on")
    assert _process_reaping_enabled(AppConfig(model="x", process_reaping_enabled=False)) is True


def test_kill_switch_config_key_roundtrip() -> None:
    cfg = AppConfig(model="x")
    assert cfg.process_reaping_enabled is True
    set_config_value(cfg, "process_reaping_enabled", "off")
    assert cfg.process_reaping_enabled is False
    set_config_value(cfg, "process_reaping_enabled", "true")
    assert cfg.process_reaping_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "process_reaping_enabled", "maybe")
