from __future__ import annotations

from collections.abc import Iterator

import pytest

from alysis_code.llm.types import LLMResponse, LLMUsage
from alysis_code.provider_telemetry import (
    ProviderCallTelemetryRecorder,
    provider_cache_diagnostics_snapshot,
    provider_cache_effectiveness_snapshot,
    reset_provider_telemetry_for_tests,
)


@pytest.fixture(autouse=True)
def isolated_telemetry() -> Iterator[None]:
    reset_provider_telemetry_for_tests()
    yield
    reset_provider_telemetry_for_tests()


def _usage(**fields: int | None) -> LLMUsage:
    values = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None, **fields}
    return LLMUsage(**values)


def _record(usage: LLMUsage | None, *, model: str = "test-model") -> None:
    recorder = ProviderCallTelemetryRecorder(
        provider_key="test-provider",
        protocol="openai_compat",
        model=model,
        base_url="https://example.invalid/v1",
        stream=False,
        tools=None,
        cache_policy={"enabled": True, "used": False},
    )
    recorder.record_success(LLMResponse(content="Done", tool_calls=[], raw={}, usage=usage))


@pytest.mark.parametrize("cached,ratio,misses", [(None, None, 0), (0, 0.0, 1), (80, 0.8, 0)])
def test_missing_cache_usage_is_distinct_from_reported_zero(
    cached: int | None, ratio: float | None, misses: int
) -> None:
    _record(_usage(prompt_tokens=100, completion_tokens=10, cached_prompt_tokens=cached))

    effectiveness = provider_cache_effectiveness_snapshot()
    diagnostics = provider_cache_diagnostics_snapshot()
    for bucket in (effectiveness["totals"], effectiveness["by_provider_model"][0]):
        assert bucket["cache_read_ratio"] == ratio
        assert bucket["cache_miss_call_count"] == misses
        assert bucket["cache_read_usage_sample_count"] == int(cached is not None)
        assert bucket["cache_read_usage_missing_call_count"] == int(cached is None)
    for bucket in (diagnostics["totals"], diagnostics["by_route"][0]):
        assert bucket["cache_read_rate"] == (None if cached is None else float(cached > 0))
        assert bucket["cache_write_rate"] is None
        assert bucket["cache_read_usage_sample_count"] == int(cached is not None)
        assert bucket["cache_read_usage_missing_call_count"] == int(cached is None)


def test_missing_response_usage_is_not_a_cache_miss() -> None:
    _record(None)

    effectiveness = provider_cache_effectiveness_snapshot()["totals"]
    diagnostics = provider_cache_diagnostics_snapshot()["totals"]
    assert effectiveness["cache_miss_call_count"] == 0
    assert effectiveness["cache_read_ratio"] is None
    assert effectiveness["cache_read_usage_missing_call_count"] == 1
    assert diagnostics["cache_read_rate"] is None
    assert diagnostics["cache_write_rate"] is None


def test_mixed_samples_use_matched_cache_usage_denominators() -> None:
    _record(_usage(prompt_tokens=1000, completion_tokens=10), model="missing-model")
    _record(_usage(prompt_tokens=100, completion_tokens=10, cached_prompt_tokens=0))
    _record(_usage(prompt_tokens=100, completion_tokens=10, cached_prompt_tokens=80))
    # A known cache count without a prompt total proves a hit, but cannot
    # contribute to the token ratio's numerator without its denominator.
    _record(_usage(completion_tokens=10, cached_prompt_tokens=50))

    snapshot = provider_cache_effectiveness_snapshot()
    totals = snapshot["totals"]
    assert totals["provider_call_count"] == 4
    assert totals["token_totals"]["prompt_tokens"] == 1200
    assert totals["token_totals"]["effective_cache_read_input_tokens"] == 130
    assert totals["cache_read_usage_sample_count"] == 3
    assert totals["cache_read_usage_missing_call_count"] == 1
    assert totals["cache_read_ratio_sample_count"] == 2
    assert totals["cache_read_ratio_prompt_tokens"] == 200
    assert totals["cache_read_ratio_cached_tokens"] == 80
    assert totals["cache_read_ratio"] == 0.4
    assert totals["cache_miss_call_count"] == 1
    missing = next(x for x in snapshot["by_provider_model"] if x["model"] == "missing-model")
    assert missing["cache_read_ratio"] is None
    assert missing["cache_miss_call_count"] == 0
    diagnostics = provider_cache_diagnostics_snapshot()["totals"]
    assert diagnostics["cache_read_rate"] == 0.6667
    assert diagnostics["cache_read_usage_sample_count"] == 3


@pytest.mark.parametrize(
    "write_fields,write_rate,write_samples,written",
    [
        ({"cache_creation_input_tokens": 0}, 0.0, 1, 0),
        ({"cache_creation_input_tokens": 70}, 1.0, 1, 70),
        ({"cache_creation_5m_input_tokens": 70}, 1.0, 1, 70),
        ({"cache_creation_5m_input_tokens": 0}, None, 0, 0),
        ({"cache_creation_5m_input_tokens": 0, "cache_creation_1h_input_tokens": 0}, 0.0, 1, 0),
    ],
)
def test_write_reporting_does_not_invent_a_read_sample(
    write_fields: dict[str, int], write_rate: float | None, write_samples: int, written: int
) -> None:
    _record(_usage(prompt_tokens=100, completion_tokens=10, **write_fields))

    totals = provider_cache_effectiveness_snapshot()["totals"]
    assert totals["cache_read_ratio"] is None
    assert totals["cache_read_usage_sample_count"] == 0
    assert totals["cache_miss_call_count"] == 0
    assert totals["token_totals"]["effective_cache_write_input_tokens"] == written
    diagnostics = provider_cache_diagnostics_snapshot()["totals"]
    assert diagnostics["cache_read_rate"] is None
    assert diagnostics["cache_write_rate"] == write_rate
    assert diagnostics["cache_write_usage_sample_count"] == write_samples
    assert diagnostics["cache_write_usage_missing_call_count"] == 1 - write_samples


def test_missing_writes_do_not_dilute_known_write_rate() -> None:
    for writes in (None, 0, 70):
        _record(_usage(prompt_tokens=100, cache_creation_input_tokens=writes))

    totals = provider_cache_diagnostics_snapshot()["totals"]
    assert totals["cache_write_rate"] == 0.5
    assert totals["cache_write_usage_sample_count"] == 2
    assert totals["cache_write_usage_missing_call_count"] == 1


def test_inconsistent_cache_count_cannot_produce_a_ratio_over_one() -> None:
    _record(_usage(prompt_tokens=100, cached_prompt_tokens=101))

    totals = provider_cache_effectiveness_snapshot()["totals"]
    assert totals["cache_read_ratio"] is None
    assert totals["cache_read_ratio_sample_count"] == 0
    assert totals["cache_read_ratio_prompt_tokens"] == 0
    assert totals["token_totals"]["effective_cache_read_input_tokens"] == 101
