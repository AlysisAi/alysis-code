from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent.session as session_mod
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.process_reaping import (
    ProcessGroupRegistry,
    ProcessReapOutcome,
    process_group_exists,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import read_session_events

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")


def _make_session(tmp_path: Path, *, runtime_kind: RuntimeKind, session_id: str):
    sessions_dir = tmp_path / "sessions"
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    # These tests exercise host process-group ownership, not a container backend.
    cfg.extra_fields = {"shell_sandbox": {"mode": "off"}}
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=runtime_kind is RuntimeKind.ONE_SHOT,
        runtime_kind=runtime_kind,
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    return session, sessions_dir


def _events(sessions_dir: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    return [
        event.get("payload") or {}
        for event in read_session_events(sessions_dir / f"{session_id}.jsonl")
        if str(event.get("type") or "") == event_type
    ]


class _FakeReaper:
    """Stands in for the signalling layer so the wiring is testable on any host."""

    def __init__(self, *, outcomes: tuple[ProcessReapOutcome, ...] = ()) -> None:
        self.outcomes = outcomes
        self.reap_calls = 0
        self.survivor_calls = 0

    def reap(self, registry, **kwargs):
        self.reap_calls += 1
        return self.outcomes

    def survivors(self, registry):
        self.survivor_calls += 1
        return tuple(outcome.payload() for outcome in self.outcomes)


def _install_fake_reaper(monkeypatch, fake: _FakeReaper) -> None:
    monkeypatch.setattr(session_mod, "reap_tracked_groups", fake.reap)
    monkeypatch.setattr(session_mod, "survivor_payloads", fake.survivors)


def _outcome(**overrides: Any) -> ProcessReapOutcome:
    base: dict[str, Any] = {
        "pgid": 4242,
        "command": "python -m pytest sklearn/tests/test_forest.py",
        "origin": "shell_run:host",
        "runtime_s": 91.4,
        "signal_used": "SIGTERM",
    }
    base.update(overrides)
    return ProcessReapOutcome(**base)


# ---------------------------------------------------------------------------
# Test 1: autonomous turn finalization reaps; interactive reports
# ---------------------------------------------------------------------------


def test_one_shot_turn_finalization_reaps_and_records(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-one-shot"
    )
    try:
        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    finally:
        session.store.close()

    assert fake.reap_calls == 1
    assert fake.survivor_calls == 0
    payloads = _events(sessions_dir, "reap-one-shot", "process_reaped")
    assert len(payloads) == 1
    assert payloads[0]["pgid"] == 4242
    assert payloads[0]["signal_used"] == "SIGTERM"
    assert payloads[0]["runtime_s"] == 91.4
    assert payloads[0]["event"] == "turn_finalization"
    assert payloads[0]["runtime_kind"] == "one_shot"
    assert _events(sessions_dir, "reap-one-shot", "process_survivors") == []


def test_interactive_turn_finalization_reports_without_reaping(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(command="npm run dev"),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.INTERACTIVE_CHAT, session_id="reap-interactive"
    )
    try:
        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    finally:
        session.store.close()

    assert fake.reap_calls == 0, "an interactive turn must never auto-kill"
    assert fake.survivor_calls == 1
    payloads = _events(sessions_dir, "reap-interactive", "process_survivors")
    assert len(payloads) == 1
    assert payloads[0]["count"] == 1
    assert payloads[0]["groups"][0]["command"] == "npm run dev"
    assert _events(sessions_dir, "reap-interactive", "process_reaped") == []


def test_interactive_session_close_reaps(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(command="npm run dev", signal_used="SIGKILL"),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.INTERACTIVE_CHAT, session_id="reap-close"
    )
    session.close()

    assert fake.reap_calls == 1
    payloads = _events(sessions_dir, "reap-close", "process_reaped")
    assert len(payloads) == 1
    assert payloads[0]["event"] == "session_close"
    assert payloads[0]["signal_used"] == "SIGKILL"


def test_kill_switch_restores_legacy_behavior(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_PROCESS_REAPING", "off")
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-off"
    )
    session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    session.close()

    assert fake.reap_calls == 0
    assert fake.survivor_calls == 0
    assert _events(sessions_dir, "reap-off", "process_reaped") == []
    assert _events(sessions_dir, "reap-off", "process_survivors") == []


def test_reaping_failure_never_breaks_the_turn(tmp_path: Path, monkeypatch) -> None:
    def _explode(registry, **kwargs):
        raise RuntimeError("signal layer is broken")

    monkeypatch.setattr(session_mod, "reap_tracked_groups", _explode)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-error"
    )
    try:
        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    finally:
        session.store.close()

    warnings = [
        payload
        for payload in _events(sessions_dir, "reap-error", "warning")
        if payload.get("warning") == "process_reaping_failed"
    ]
    assert len(warnings) == 1
    assert "signal layer is broken" in warnings[0]["error"]


# ---------------------------------------------------------------------------
# Test 2: the turn boundary fires on every exit path
# ---------------------------------------------------------------------------


def test_turn_finalization_fires_even_when_the_turn_raises(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-raise"
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(session_mod, "_run_turn", _boom)
    try:
        with pytest.raises(RuntimeError, match="provider exploded"):
            session.run_turn("do the thing")
    finally:
        session.store.close()

    assert fake.reap_calls == 1


def test_turn_finalization_fires_on_cancellation(tmp_path: Path, monkeypatch) -> None:
    # KeyboardInterrupt is a BaseException, so an `except Exception` boundary
    # would miss it; the reap hook must be in a `finally`.
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-cancel"
    )

    def _cancel(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(session_mod, "_run_turn", _cancel)
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn("do the thing")
    finally:
        session.store.close()

    assert fake.reap_calls == 1


def test_turn_finalization_fires_on_success(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-success"
    )
    monkeypatch.setattr(session_mod, "_run_turn", lambda *_a, **_k: 0)
    try:
        assert session.run_turn("do the thing") == 0
    finally:
        session.store.close()

    assert fake.reap_calls == 1


# ---------------------------------------------------------------------------
# Test 3: end to end with a real process group (POSIX)
# ---------------------------------------------------------------------------


@posix_only
def test_real_orphan_is_reaped_at_one_shot_turn_finalization(tmp_path: Path) -> None:
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-real"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        registry = session.process_group_registry
        assert isinstance(registry, ProcessGroupRegistry)
        pgid = os.getpgid(child.pid)
        assert registry.register(pgid=pgid, command="pytest -q", origin="shell_run:host")

        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)

        child.wait(timeout=15)
        assert not process_group_exists(pgid)
        payloads = _events(sessions_dir, "reap-real", "process_reaped")
        assert len(payloads) == 1
        assert payloads[0]["pgid"] == pgid
        assert payloads[0]["command"] == "pytest -q"
        assert payloads[0]["signal_used"] in {"SIGTERM", "SIGKILL"}
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        session.store.close()


@posix_only
def test_real_interactive_survivor_is_reported_then_reaped_at_close(tmp_path: Path) -> None:
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.INTERACTIVE_CHAT, session_id="reap-real-interactive"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(child.pid)
        assert session.process_group_registry is not None
        session.process_group_registry.register(
            pgid=pgid, command="npm run dev", origin="shell_run:host"
        )

        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
        time.sleep(0.2)
        assert child.poll() is None, "an interactive turn killed the user's dev server"

        session.close()
        child.wait(timeout=15)
        assert not process_group_exists(pgid)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    survivors = _events(sessions_dir, "reap-real-interactive", "process_survivors")
    assert len(survivors) == 1
    assert survivors[0]["groups"][0]["command"] == "npm run dev"
    reaped = _events(sessions_dir, "reap-real-interactive", "process_reaped")
    assert len(reaped) == 1
    assert reaped[0]["event"] == "session_close"


@posix_only
def test_session_shell_runner_places_commands_in_their_own_group(tmp_path: Path) -> None:
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-runner"
    )
    try:
        runner = session.shell_runner
        assert runner is not None
        completed = runner.run(root=tmp_path, cwd=tmp_path, cmd="ps -o pgid= -p $$", timeout_s=30)
        assert completed.returncode == 0
        child_pgid = int(completed.stdout.strip())
        assert child_pgid != os.getpgrp(), "the command ran in the agent's own process group"
    finally:
        session.close()
