"""Durable provider/web-search telemetry sink.

The in-memory telemetry deques evaporate on process exit -- exactly when a crashed
run is investigated. A registered JSONL sink persists each already-redacted summary
to disk so the retry/throttle/latency history is reconstructable from artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from alysis_code.provider_telemetry import (
    provider_telemetry_sink_path,
    record_provider_call,
    record_web_search_call,
    reset_provider_telemetry_for_tests,
    set_provider_telemetry_sink,
)


def test_provider_telemetry_sink_persists_redacted_jsonl(tmp_path: Path) -> None:
    reset_provider_telemetry_for_tests()
    sink = tmp_path / "diagnostics" / "provider_telemetry.jsonl"
    set_provider_telemetry_sink(sink)
    try:
        record_provider_call(
            {
                "kind": "provider_call",
                "operation": "chat",
                "model": "deepseek-v4-pro",
                "status_category": "rate_limited",
                "retry_count": 2,
                "api_key": "sk-SHOULDBEREDACTED1234567",
            }
        )
        record_web_search_call(
            protocol="openai",
            provider_key="deepseek",
            model="deepseek-v4-pro",
            web_search_mode="auto",
            web_search_adapter="tavily",
            provider_hosted_search=False,
            external_provider_name="tavily",
            source_count=3,
            citation_count=1,
            query_count=1,
            fallback_occurred=False,
        )
    finally:
        set_provider_telemetry_sink(None)

    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    provider_record = json.loads(lines[0])
    assert provider_record["kind"] == "provider_call"
    assert provider_record["status_category"] == "rate_limited"
    assert provider_record["model"] == "deepseek-v4-pro"
    assert "recorded_at_epoch" in provider_record
    # The sensitive key is redacted before it touches disk.
    assert provider_record["api_key"] == "[redacted]"

    web_record = json.loads(lines[1])
    assert web_record["kind"] == "web_search"
    reset_provider_telemetry_for_tests()


def test_provider_telemetry_sink_disabled_by_default_writes_nothing(tmp_path: Path) -> None:
    reset_provider_telemetry_for_tests()
    assert provider_telemetry_sink_path() is None

    record_provider_call({"kind": "provider_call", "model": "x", "status_category": "success"})

    assert list(tmp_path.iterdir()) == []
