from __future__ import annotations

import math

import pytest

from alysis_code.execution_deadline import (
    DeadlineExhausted,
    DeadlineOperation,
    DeadlinePhase,
    DeadlineSource,
    ExecutionDeadline,
    deadline_timeout_or_raise,
    derive_subagent_deadline,
    temporarily_clamp_client_timeout,
    validate_deadline_seconds,
)


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_deadline_none_is_disabled() -> None:
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(None, clock=clock)

    assert deadline.enabled is False
    assert deadline.remaining_seconds() is None
    assert deadline.is_exhausted() is False
    assert deadline.can_start(10_000) is True
    assert deadline.clamp_timeout(30.0) == 30.0
    assert deadline.telemetry_snapshot()["enabled"] is False


def test_deadline_tracks_elapsed_remaining_and_exhaustion() -> None:
    clock = _FakeClock(10.0)
    deadline = ExecutionDeadline.from_duration(5.0, clock=clock)

    assert deadline.started_at_monotonic == 10.0
    assert deadline.deadline_monotonic == 15.0
    assert deadline.elapsed_seconds() == 0.0
    assert deadline.remaining_seconds() == 5.0
    assert deadline.can_start(4.9) is True

    clock.advance(3.25)
    assert deadline.elapsed_seconds() == 3.25
    assert deadline.remaining_seconds() == 1.75
    assert deadline.can_start(2.0) is False

    clock.advance(2.0)
    assert deadline.remaining_seconds() == 0.0
    assert deadline.is_exhausted() is True


def test_deadline_finalization_phase_uses_observed_latency_and_source() -> None:
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(
        20.0,
        clock=clock,
        source=DeadlineSource.EXPLICIT_CLI,
    )

    assert deadline.phase() == DeadlinePhase.NORMAL
    assert deadline.finalization_reserve_seconds() == 1.0

    deadline.observe_duration(DeadlineOperation.MAIN_LLM, 4.0)
    clock.advance(16.0)

    assert deadline.finalization_reserve_seconds() == 5.0
    assert deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW
    assert deadline.normal_work_remaining_seconds() == 0.0
    assert deadline.maybe_enter_finalization("reserve_reached") is True
    assert deadline.maybe_enter_finalization("later") is False

    blocked = deadline.start_decision(
        DeadlineOperation.SUBAGENT,
        minimum_remaining_seconds=2.0,
    )
    assert blocked.allowed is False
    assert blocked.reason == "finalization_disallows_operation"

    allowed = deadline.start_decision(
        DeadlineOperation.MUTATION_TOOL,
        minimum_remaining_seconds=0.05,
        allow_during_finalization=True,
    )
    assert allowed.allowed is True
    assert allowed.reason == "finalization_allowed"

    snapshot = deadline.telemetry_snapshot()
    assert snapshot["source"] == "explicit_cli"
    assert snapshot["phase"] == "finalization_window"
    assert snapshot["finalization_reason"] == "reserve_reached"
    assert snapshot["duration_observations"]["main_llm"]["count"] == 1


def test_deadline_admission_uses_recent_median_instead_of_one_latency_outlier() -> None:
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(1_000.0, clock=clock)
    for duration in (72.0, 81.0, 201.0, 64.0, 88.0):
        deadline.observe_duration(DeadlineOperation.MAIN_LLM, duration)
    clock.advance(684.0)

    decision = deadline.start_decision(
        DeadlineOperation.MAIN_LLM,
        minimum_remaining_seconds=0.25,
    )

    assert deadline.remaining_seconds() == 316.0
    assert deadline.normal_work_remaining_seconds() == 196.0
    assert decision.allowed is True
    assert decision.estimated_duration_seconds == 81.0
    observation = deadline.telemetry_snapshot()["duration_observations"]["main_llm"]
    assert observation["estimate_strategy"] == "median_recent"
    assert observation["estimate_window_seconds"] == [72.0, 81.0, 201.0, 64.0, 88.0]
    assert observation["estimated_seconds"] == 81.0

    clock.advance(210.0)
    assert deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW


def test_deadline_refuses_when_recent_median_exceeds_total_remaining() -> None:
    clock = _FakeClock(0.0)
    deadline = ExecutionDeadline.from_duration(1_000.0, clock=clock)
    for duration in (210.0, 220.0, 230.0):
        deadline.observe_duration(DeadlineOperation.MAIN_LLM, duration)
    clock.advance(800.0)

    decision = deadline.start_decision(DeadlineOperation.MAIN_LLM)

    assert deadline.remaining_seconds() == 200.0
    assert decision.allowed is False
    assert decision.reason == "insufficient_hard_remaining"
    assert decision.estimated_duration_seconds == 220.0


def test_deadline_clamps_timeouts_with_cleanup_reserve() -> None:
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(10.0, clock=clock)

    assert deadline.clamp_timeout(30.0, reserve_seconds=1.0) == 9.0
    assert deadline.clamp_timeout(3.0, reserve_seconds=1.0) == 3.0
    clock.advance(9.98)
    assert deadline.clamp_timeout(30.0, reserve_seconds=1.0) is None

    with pytest.raises(DeadlineExhausted):
        deadline_timeout_or_raise(
            deadline,
            30.0,
            reserve_seconds=1.0,
            operation="test_operation",
        )


def test_absolute_deadline_is_shared_by_children_without_drift() -> None:
    clock = _FakeClock(50.0)
    parent = ExecutionDeadline.from_duration(12.0, clock=clock)
    clock.advance(4.0)

    child = ExecutionDeadline.from_absolute(
        started_at_monotonic=parent.started_at_monotonic,
        deadline_monotonic=parent.deadline_monotonic,
        configured_duration_seconds=parent.configured_duration_seconds,
        clock=clock,
    )

    assert child.deadline_monotonic == parent.deadline_monotonic
    assert child.remaining_seconds() == parent.remaining_seconds() == 8.0


def test_subagent_deadline_uses_finite_fallback_without_parent() -> None:
    child = derive_subagent_deadline(None, 900.0)

    assert child.enabled is True
    assert child.configured_duration_seconds == 900.0
    assert child.source == DeadlineSource.SUBAGENT_FALLBACK
    remaining = child.remaining_seconds()
    assert remaining is not None
    assert 899.0 <= remaining <= 900.0 + 1e-6


def test_subagent_deadline_reuses_exact_earlier_parent_object() -> None:
    clock = _FakeClock(10.0)
    parent = ExecutionDeadline.from_duration(20.0, clock=clock)

    child = derive_subagent_deadline(parent, 900.0)

    assert child is parent
    assert child.deadline_monotonic == 30.0


def test_subagent_deadline_caps_disabled_parent_with_same_clock_and_policy() -> None:
    clock = _FakeClock(10.0)
    parent = ExecutionDeadline.from_duration(None, clock=clock)

    child = derive_subagent_deadline(parent, 30.0)

    assert child is not parent
    assert child.deadline_monotonic == 40.0
    assert child.remaining_seconds() == 30.0
    assert child.source == DeadlineSource.SUBAGENT_FALLBACK
    assert child.finalization_policy is parent.finalization_policy
    clock.advance(4.0)
    assert child.remaining_seconds() == 26.0


def test_subagent_deadline_caps_later_active_parent() -> None:
    clock = _FakeClock(10.0)
    parent = ExecutionDeadline.from_duration(100.0, clock=clock)

    child = derive_subagent_deadline(parent, 30.0)

    assert child is not parent
    assert child.deadline_monotonic == 40.0
    assert child.remaining_seconds() == 30.0
    assert child.source == DeadlineSource.SUBAGENT_FALLBACK
    assert child.finalization_policy is parent.finalization_policy


def test_subagent_deadline_reuses_later_parent_already_in_finalization() -> None:
    clock = _FakeClock(0.0)
    parent = ExecutionDeadline.from_duration(20.0, clock=clock)
    parent.observe_duration(DeadlineOperation.MAIN_LLM, 4.0)
    clock.advance(16.0)

    assert parent.phase() == DeadlinePhase.FINALIZATION_WINDOW
    assert parent.deadline_monotonic == 20.0
    child = derive_subagent_deadline(parent, 2.0)

    assert child is parent
    assert child.deadline_monotonic > clock() + 2.0
    decision = child.start_decision(
        DeadlineOperation.SUBAGENT,
        minimum_remaining_seconds=2.0,
    )
    assert decision.allowed is False
    assert decision.reason == "finalization_disallows_operation"


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "-inf"])
def test_deadline_validation_rejects_non_positive_or_non_finite(value: object) -> None:
    with pytest.raises(ValueError, match="finite number > 0"):
        validate_deadline_seconds(value)


def test_deadline_validation_accepts_valid_float() -> None:
    assert validate_deadline_seconds("12.5") == 12.5


def test_temporarily_clamp_client_timeout_restores_original_value() -> None:
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(5.0, clock=clock)
    client = type("Client", (), {"timeout_s": 60.0})()

    with temporarily_clamp_client_timeout(client, deadline, reserve_seconds=1.0):
        # Outside the finalization window the clamp also protects the
        # finalization reserve (1.0s here), not only the cleanup reserve:
        # 5.0 remaining - 1.0 cleanup - 1.0 finalization reserve = 3.0.
        assert math.isclose(client.timeout_s, 3.0)

    assert client.timeout_s == 60.0


def test_stream_deadline_checker_honors_grace_and_is_restored() -> None:
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(5.0, clock=clock)
    client = type(
        "Client",
        (),
        {"timeout_s": 60.0, "inflight_deadline_grace_s": 2.0},
    )()

    with temporarily_clamp_client_timeout(client, deadline, reserve_seconds=1.0):
        clock.advance(6.9)
        assert client._stream_deadline_exhausted() is False
        clock.advance(0.1)
        assert client._stream_deadline_exhausted() is True

    assert not hasattr(client, "_stream_deadline_exhausted")


def test_finalization_provider_retries_continue_while_hard_deadline_allows() -> None:
    clock = _FakeClock(now=0.0)
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0.0,
        deadline_monotonic=0.8,
        configured_duration_seconds=10.0,
        clock=clock,
    )
    client = type("Client", (), {"timeout_s": 60.0})()

    assert deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW

    with temporarily_clamp_client_timeout(
        client,
        deadline,
        reserve_seconds=0.1,
        minimum_timeout_seconds=0.05,
    ):
        assert client._provider_retry_deadline_allows(0.1) is True

    with temporarily_clamp_client_timeout(
        client,
        deadline,
        reserve_seconds=0.1,
        minimum_timeout_seconds=0.05,
    ):
        assert client._provider_retry_deadline_allows(0.1) is True

    clock.advance(0.7)
    with temporarily_clamp_client_timeout(
        client,
        deadline,
        reserve_seconds=0.01,
        minimum_timeout_seconds=0.05,
    ):
        assert client._provider_retry_deadline_allows(0.1) is False
