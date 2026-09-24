from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code import agent_loop
from alysis_code.config import AppConfig, ConfigError
from alysis_code.execution_deadline import (
    DeadlineExhausted,
    DeadlineOperation,
    ExecutionDeadline,
    temporarily_clamp_client_timeout,
)
from alysis_code.llm.types import LLMError
from alysis_code.managed_host_deadline import (
    MANAGED_HOST_DEADLINE_UNIX_ENV,
    ManagedHostDeadlineError,
    clamp_to_managed_host_deadline,
    managed_host_deadline_anchor_unix_seconds,
)


@pytest.mark.parametrize("existing,expected", [(None, 110), (120, 110), (103, 103), (99, 99)])
def test_launch_anchor_never_renews_inherited_expiry(existing: Any, expected: float) -> None:
    assert (
        managed_host_deadline_anchor_unix_seconds(
            10, existing_anchor=existing, now_unix_seconds=100
        )
        == expected
    )


@pytest.mark.parametrize("invalid", ["", "no", "nan", "inf", "-inf", 0, -1])
def test_invalid_anchor_fails_closed(invalid: Any) -> None:
    with pytest.raises(ManagedHostDeadlineError, match="finite number"):
        managed_host_deadline_anchor_unix_seconds(10, existing_anchor=invalid, now_unix_seconds=100)
    deadline = ExecutionDeadline.from_duration(10, clock=lambda: 50)
    with pytest.raises(ManagedHostDeadlineError, match="finite number"):
        clamp_to_managed_host_deadline(deadline, invalid, now_unix_seconds=100)
    assert deadline.deadline_monotonic == 60


def test_startup_consumes_existing_budget_and_conversion_uses_monotonic_afterward() -> None:
    clock = [50.0]
    deadline = ExecutionDeadline.from_duration(10, clock=lambda: clock[0], source="explicit_cli")
    deadline.observe_duration(DeadlineOperation.MAIN_LLM, 0.1)
    observations = deadline._duration_observations
    policy = deadline.finalization_policy
    record = clamp_to_managed_host_deadline(deadline, 102, now_unix_seconds=100)
    assert deadline.deadline_monotonic == 52
    assert deadline.started_at_monotonic == 42
    assert deadline.configured_duration_seconds == 10
    assert deadline.elapsed_fraction() == pytest.approx(0.8)
    assert deadline._duration_observations is observations
    assert deadline.finalization_policy is policy
    assert record["anchor_source"] == MANAGED_HOST_DEADLINE_UNIX_ENV
    assert record["deadline_source"] == "explicit_cli"
    assert record["narrowed_by_seconds"] == 8
    clock[0] = 51
    assert deadline.remaining_seconds() == 1
    clock[0] = 52
    assert deadline.is_exhausted()


def test_earlier_parent_deadline_and_elapsed_span_are_preserved() -> None:
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=40,
        deadline_monotonic=51,
        clock=lambda: 50,
    )
    record = clamp_to_managed_host_deadline(deadline, 120, now_unix_seconds=100)
    assert deadline.deadline_monotonic == 51
    assert deadline.started_at_monotonic == 40
    assert record["narrowed_by_seconds"] == 0
    assert record["deadline_source"] == "inherited_parent"


def test_expired_anchor_is_not_a_new_relative_budget() -> None:
    deadline = ExecutionDeadline.from_duration(10, clock=lambda: 50)
    record = clamp_to_managed_host_deadline(deadline, 99, now_unix_seconds=100)
    assert deadline.is_exhausted()
    assert not deadline.start_decision(DeadlineOperation.MAIN_LLM).allowed
    assert record["status"] == "exhausted"


def test_absolute_anchor_cannot_supply_a_missing_relative_deadline() -> None:
    with pytest.raises(ManagedHostDeadlineError, match="finite run deadline"):
        clamp_to_managed_host_deadline(
            ExecutionDeadline.from_duration(None), 110, now_unix_seconds=100
        )


@pytest.mark.parametrize("required", [True, False])
def test_runtime_consumes_anchor_only_for_explicit_managed_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    monkeypatch.setenv(MANAGED_HOST_DEADLINE_UNIX_ENV, "1")
    captured: dict[str, Any] = {}
    records: list[tuple[str, dict[str, Any]]] = []

    class Session:
        store = SimpleNamespace(append=lambda kind, payload: records.append((kind, payload)))
        crash_diagnostics = None

        def run_turn(self, *_: Any, **__: Any) -> int:
            assert captured["execution_deadline"].is_exhausted() is required
            return 0

        def close(self) -> None:
            pass

    def create(**kwargs: Any) -> Session:
        captured.update(kwargs)
        return Session()

    monkeypatch.setattr(agent_loop, "create_session", create)
    assert (
        agent_loop.run_agent(
            cfg=AppConfig(model="test-model"),
            root=tmp_path,
            instruction="bounded work",
            mode="fullaccess",
            yes=True,
            max_steps=1,
            no_log=True,
            run_deadline_seconds=10,
            require_run_deadline=required,
        )
        == 0
    )
    anchors = [payload for kind, payload in records if kind == "managed_host_deadline_anchor"]
    assert bool(anchors) is required
    if required:
        assert anchors[0]["status"] == "exhausted"


def test_runtime_rejects_malformed_host_anchor_before_session_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MANAGED_HOST_DEADLINE_UNIX_ENV, "nan")
    monkeypatch.setattr(agent_loop, "create_session", lambda **_: pytest.fail("must not start"))
    with pytest.raises(ConfigError, match="managed_host_deadline_unix_seconds"):
        agent_loop.run_agent(
            cfg=AppConfig(model="test-model"),
            root=tmp_path,
            instruction="bounded work",
            mode="fullaccess",
            yes=True,
            max_steps=1,
            no_log=True,
            run_deadline_seconds=10,
            require_run_deadline=True,
        )


@pytest.mark.parametrize(
    "configured,elapsed,error,expected",
    [
        (60, 8, httpx.ReadTimeout("read stalled"), DeadlineExhausted),
        (60, 8, TimeoutError("TLS stalled"), DeadlineExhausted),
        (60, 7.9, httpx.ReadTimeout("early provider timeout"), LLMError),
        (1, 1, httpx.ReadTimeout("provider timeout"), LLMError),
        (1, 10, httpx.ReadTimeout("own timeout near host expiry"), LLMError),
        (60, 10, RuntimeError("HTTP 503"), LLMError),
        (60, 10, RuntimeError("message merely says timed out"), LLMError),
    ],
)
def test_only_expired_host_capped_transport_timeouts_become_deadline_outcomes(
    configured: float, elapsed: float, error: Exception, expected: type[Exception]
) -> None:
    clock = [0.0]
    deadline = ExecutionDeadline.from_duration(10, clock=lambda: clock[0])
    client = SimpleNamespace(timeout_s=configured)
    with pytest.raises(expected):
        with temporarily_clamp_client_timeout(client, deadline):
            clock[0] += elapsed
            raise LLMError("normalized provider failure") from error
    assert client.timeout_s == configured
    assert not hasattr(client, "_provider_retry_deadline_allows")


def test_retry_becomes_host_limited_without_renewing_the_deadline() -> None:
    clock = [0.0]
    deadline = ExecutionDeadline.from_duration(20, clock=lambda: clock[0])
    client = SimpleNamespace(timeout_s=5)
    with pytest.raises(DeadlineExhausted):
        with temporarily_clamp_client_timeout(client, deadline):
            clock[0] = 14
            assert client._provider_retry_deadline_allows(0)
            assert client.timeout_s < 5
            clock[0] += client.timeout_s
            raise LLMError("normalized provider failure") from httpx.ReadTimeout("stalled retry")
    assert client.timeout_s == 5


def test_provider_response_status_wins_over_an_older_timeout_context() -> None:
    clock = [0.0]
    deadline = ExecutionDeadline.from_duration(10, clock=lambda: clock[0])
    client = SimpleNamespace(timeout_s=60)
    response_error = LLMError("provider rejected the request")
    response_error.provider_status_code = 503
    with pytest.raises(LLMError) as raised:
        with temporarily_clamp_client_timeout(client, deadline):
            clock[0] = 10
            raise response_error from httpx.ReadTimeout("earlier abandoned request")
    assert raised.value is response_error
