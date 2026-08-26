"""Per-attempt LLM timeout ceiling and reserve-aware clamping.

One hung provider read must not consume the remaining run budget (observed in
scored runs: a single read blocked for ~60 minutes until the wall clock died).
The ceiling bounds each attempt so the provider-retry ladder gets a chance, and
outside the finalization window the clamp keeps the finalization reserve
untouched so the wrap-up call can still happen after a timeout.
"""

from __future__ import annotations

import math

from alysis_code.execution_deadline import (
    DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS,
    DeadlinePhase,
    ExecutionDeadline,
    resolve_llm_attempt_timeout_ceiling_seconds,
    temporarily_clamp_client_timeout,
)

_ENV = "ALYSIS_LLM_ATTEMPT_TIMEOUT_S"


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client(timeout_s: float | None = 86400.0):
    return type("Client", (), {"timeout_s": timeout_s})()


def test_ceiling_defaults_to_900_seconds(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert resolve_llm_attempt_timeout_ceiling_seconds() == (
        DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS
    )
    assert DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS == 900.0


def test_ceiling_env_override_and_off_words(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "300")
    assert resolve_llm_attempt_timeout_ceiling_seconds() == 300.0
    for word in ("off", "unlimited", "none", "never", "0", "disabled", "-5"):
        monkeypatch.setenv(_ENV, word)
        assert resolve_llm_attempt_timeout_ceiling_seconds() is None, word
    monkeypatch.setenv(_ENV, "not-a-number")
    assert resolve_llm_attempt_timeout_ceiling_seconds() == (
        DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS
    )
    monkeypatch.setenv(_ENV, "")
    assert resolve_llm_attempt_timeout_ceiling_seconds() == (
        DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS
    )


def test_clamp_applies_attempt_ceiling_with_huge_configured_timeout(monkeypatch) -> None:
    """The benchmark operator's 86400s request timeout no longer survives the clamp."""
    monkeypatch.delenv(_ENV, raising=False)
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(3600.0, clock=clock)
    client = _client(86400.0)

    with temporarily_clamp_client_timeout(client, deadline, operation="main_llm"):
        assert client.timeout_s == 900.0

    assert client.timeout_s == 86400.0


def test_clamp_env_ceiling_wins_when_smaller(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "120")
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(3600.0, clock=clock)
    client = _client(86400.0)

    with temporarily_clamp_client_timeout(client, deadline, operation="main_llm"):
        assert client.timeout_s == 120.0


def test_clamp_ceiling_disabled_restores_previous_behavior(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "off")
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(5.0, clock=clock)
    client = _client(60.0)

    with temporarily_clamp_client_timeout(client, deadline, reserve_seconds=1.0):
        # Reserve-aware bound still applies (5 - 1 cleanup - 1 finalization
        # reserve), but no attempt ceiling caps it further.
        assert math.isclose(client.timeout_s, 3.0)


def test_clamp_preserves_finalization_reserve_in_normal_phase(monkeypatch) -> None:
    """A request started mid-run cannot eat the finalization window."""
    monkeypatch.delenv(_ENV, raising=False)
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(1000.0, clock=clock)
    # A prior slow LLM call inflates the observed reserve.
    deadline.observe_duration("main_llm", 40.0)
    reserve = deadline.finalization_reserve_seconds()
    assert reserve > 1.0
    clock.advance(900.0)  # 100s remaining, still NORMAL phase
    assert deadline.phase() not in (DeadlinePhase.FINALIZATION_WINDOW, DeadlinePhase.EXHAUSTED)
    client = _client(86400.0)

    with temporarily_clamp_client_timeout(client, deadline, operation="main_llm"):
        expected = 100.0 - 1.0 - reserve
        assert math.isclose(client.timeout_s, expected, rel_tol=1e-3)


def test_retry_gate_shrinks_the_next_attempt_to_the_remaining_budget(monkeypatch) -> None:
    """A retry late in the run must not reuse the entry-time timeout: the next
    attempt is shrunk to what actually remains (sleep and cleanup deducted)."""
    monkeypatch.delenv(_ENV, raising=False)
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(3600.0, clock=clock)
    client = _client(86400.0)

    with temporarily_clamp_client_timeout(client, deadline, operation="main_llm"):
        assert client.timeout_s == 900.0
        clock.advance(3585.0)  # 15s remaining
        allowed = client._provider_retry_deadline_allows(2.0)
        assert allowed is True
        # Next attempt must fit inside remaining - sleep - cleanup reserve.
        assert client.timeout_s <= 15.0 - 2.0 - 1.0 + 1e-6
        assert client.timeout_s > 0.0


def test_retry_gate_protects_a_learned_finalization_reserve(monkeypatch) -> None:
    """With a 60s learned reserve and 100s remaining, the next attempt must fit
    under remaining - sleep - cleanup - reserve (~37s), never ~97s."""
    monkeypatch.delenv(_ENV, raising=False)
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(3600.0, clock=clock)
    deadline.observe_duration("main_llm", 40.0)  # learned reserve: 40 * 1.5 = 60s
    assert math.isclose(deadline.finalization_reserve_seconds(), 60.0)
    client = _client(86400.0)

    with temporarily_clamp_client_timeout(client, deadline, operation="main_llm"):
        clock.advance(3500.0)  # 100s remaining
        assert client._provider_retry_deadline_allows(2.0) is True
        assert client.timeout_s <= 100.0 - 2.0 - 1.0 - 60.0 + 1e-6
        # Refuse outright once the reserve would be consumed.
        clock.advance(36.0)  # 64s remaining; budget = 64 - 2 - 61 = 1s (allowed)
        clock.advance(3.0)  # 61s remaining; budget < floor
        assert client._provider_retry_deadline_allows(2.0) is False


def test_retry_gate_refuses_when_nothing_useful_remains(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(3600.0, clock=clock)
    client = _client(86400.0)

    with temporarily_clamp_client_timeout(client, deadline, operation="main_llm"):
        clock.advance(3599.5)  # 0.5s remaining
        assert client._provider_retry_deadline_allows(2.0) is False


def test_clamp_falls_back_to_cleanup_reserve_when_normal_window_too_small(monkeypatch) -> None:
    """Near the end, behavior matches the pre-change clamp instead of raising."""
    monkeypatch.delenv(_ENV, raising=False)
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0.0,
        deadline_monotonic=0.8,
        configured_duration_seconds=10.0,
        clock=clock,
    )
    assert deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW
    client = _client(60.0)

    with temporarily_clamp_client_timeout(
        client,
        deadline,
        reserve_seconds=0.1,
        minimum_timeout_seconds=0.05,
    ):
        # Finalization window: plain cleanup-reserve clamp, exactly as before.
        assert math.isclose(client.timeout_s, 0.7, rel_tol=1e-6)
