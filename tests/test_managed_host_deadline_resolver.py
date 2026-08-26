from __future__ import annotations

import pytest

from alysis_code.managed_host_deadline import (
    DEFAULT_MANAGED_HOST_SHUTDOWN_RESERVE_SECONDS,
    ManagedHostDeadlineError,
    resolve_managed_host_deadline,
)


def test_managed_host_deadline_subtracts_reserve() -> None:
    resolved = resolve_managed_host_deadline(
        final_effective_host_agent_timeout_seconds=600,
        host_shutdown_reserve_seconds=45,
        timeout_source="agent_kwarg:managed_host_agent_timeout_sec",
        reserve_source="agent_kwarg:managed_host_shutdown_reserve_sec",
    )

    assert resolved.alysis_invocation_deadline_seconds == 555
    assert resolved.host_remaining_timeout_seconds == 600
    assert resolved.host_shutdown_reserve_seconds == 45
    assert resolved.require_deadline is True


def test_managed_host_deadline_preserves_fractional_values_and_elapsed_time() -> None:
    resolved = resolve_managed_host_deadline(
        final_effective_host_agent_timeout_seconds="90.75",
        host_shutdown_reserve_seconds="7.25",
        elapsed_before_launch_seconds=3.5,
    )

    assert resolved.alysis_invocation_deadline_seconds == 80.0
    assert resolved.host_remaining_timeout_seconds == 87.25


def test_managed_host_deadline_uses_already_effective_timeout_without_second_multiplier() -> None:
    resolved = resolve_managed_host_deadline(
        final_effective_host_agent_timeout_seconds=2100,
        host_shutdown_reserve_seconds=30,
        timeout_source="agent_kwarg:managed_host_agent_timeout_sec",
    )

    assert resolved.final_effective_host_agent_timeout_seconds == 2100
    assert resolved.alysis_invocation_deadline_seconds == 2070


def test_managed_host_deadline_default_reserve_is_host_owned() -> None:
    resolved = resolve_managed_host_deadline(
        final_effective_host_agent_timeout_seconds=120,
    )

    assert resolved.host_shutdown_reserve_seconds == DEFAULT_MANAGED_HOST_SHUTDOWN_RESERVE_SECONDS
    assert resolved.alysis_invocation_deadline_seconds == 90


@pytest.mark.parametrize(
    "value",
    [None, 0, -1, float("inf"), float("-inf"), float("nan"), "not-a-number"],
)
def test_managed_host_deadline_rejects_missing_or_invalid_host_timeout(value: object) -> None:
    with pytest.raises(ManagedHostDeadlineError, match="final_effective_host_agent_timeout"):
        resolve_managed_host_deadline(
            final_effective_host_agent_timeout_seconds=value,
            host_shutdown_reserve_seconds=1,
        )


@pytest.mark.parametrize("reserve", [-1, float("inf"), float("nan"), "bad"])
def test_managed_host_deadline_rejects_invalid_reserve(reserve: object) -> None:
    with pytest.raises(ManagedHostDeadlineError, match="host_shutdown_reserve_seconds"):
        resolve_managed_host_deadline(
            final_effective_host_agent_timeout_seconds=10,
            host_shutdown_reserve_seconds=reserve,
        )


@pytest.mark.parametrize("reserve", [10, 11])
def test_managed_host_deadline_rejects_reserve_that_consumes_timeout(
    reserve: float,
) -> None:
    with pytest.raises(ManagedHostDeadlineError) as exc_info:
        resolve_managed_host_deadline(
            final_effective_host_agent_timeout_seconds=10,
            host_shutdown_reserve_seconds=reserve,
        )

    assert exc_info.value.code == "host_shutdown_reserve_consumes_timeout"
    assert exc_info.value.record["validation_error"] == "host_shutdown_reserve_consumes_timeout"


def test_managed_host_deadline_rejects_elapsed_time_that_leaves_too_little_budget() -> None:
    with pytest.raises(ManagedHostDeadlineError) as exc_info:
        resolve_managed_host_deadline(
            final_effective_host_agent_timeout_seconds=10,
            host_shutdown_reserve_seconds=4,
            elapsed_before_launch_seconds=5.5,
            minimum_invocation_seconds=1,
        )

    assert exc_info.value.code == "remaining_duration_too_small"
    assert exc_info.value.record["alysis_invocation_deadline_seconds"] == 0.5


def test_managed_host_deadline_rejects_negative_elapsed_time() -> None:
    with pytest.raises(ManagedHostDeadlineError, match="elapsed_before_launch_seconds"):
        resolve_managed_host_deadline(
            final_effective_host_agent_timeout_seconds=10,
            host_shutdown_reserve_seconds=1,
            elapsed_before_launch_seconds=-0.1,
        )


def test_managed_host_deadline_diagnostic_record_is_sanitized_metadata_only() -> None:
    resolved = resolve_managed_host_deadline(
        final_effective_host_agent_timeout_seconds=60,
        host_shutdown_reserve_seconds=5,
        elapsed_before_launch_seconds=1.25,
        timeout_source="agent_kwarg:managed_host_agent_timeout_sec",
        reserve_source="environment:ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC",
    )

    record = resolved.diagnostic_record()

    assert record == {
        "schema_version": 1,
        "status": "ok",
        "timeout_source": "agent_kwarg:managed_host_agent_timeout_sec",
        "reserve_source": "environment:ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC",
        "final_effective_host_agent_timeout_seconds": 60.0,
        "elapsed_before_launch_seconds": 1.25,
        "host_shutdown_reserve_seconds": 5.0,
        "alysis_invocation_deadline_seconds": 53.75,
        "require_deadline": True,
    }
    assert "instruction" not in record
    assert "api_key" not in record
    assert "environment" not in record
