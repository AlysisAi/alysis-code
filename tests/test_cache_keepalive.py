from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

import httpx

from alysis_code.agent.cache_keepalive import (
    CacheKeepaliveRequest,
    ParentCacheKeepalive,
    cache_keepalive_unsupported_reason,
)
from alysis_code.agent.session import AgentSession
from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.execution_deadline import DeadlinePhase
from alysis_code.llm.openai_responses import (
    _RESPONSES_OMIT_TEMPERATURE_MODELS,
    OpenAIResponsesClient,
    _responses_temperature_omit_key,
)
from alysis_code.llm.types import LLMResponse, LLMUsage
from alysis_code.provider_telemetry import (
    ProviderCallTelemetryRecorder,
    last_provider_call_summary,
    provider_call_history_snapshot,
    reset_provider_telemetry_for_tests,
)


class _Deadline:
    def __init__(self, *, phase: DeadlinePhase, remaining_s: float) -> None:
        self._phase = phase
        self._remaining_s = remaining_s

    def phase(self) -> DeadlinePhase:
        return self._phase

    def remaining_seconds(self) -> float:
        return self._remaining_s


class _Store:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


class _UsageRecord:
    def to_payload(self) -> dict[str, Any]:
        return {"role": "main:cache_keepalive", "total_tokens": 13}


class _KeepaliveClient:
    model = "test-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(
            {
                **kwargs,
                "messages": copy.deepcopy(kwargs["messages"]),
                "tools": copy.deepcopy(kwargs["tools"]),
                **(
                    {"tool_choice": copy.deepcopy(kwargs["tool_choice"])}
                    if "tool_choice" in kwargs
                    else {}
                ),
            }
        )
        response = LLMResponse(
            content="discard me",
            tool_calls=[],
            raw={},
            usage=LLMUsage(prompt_tokens=12, completion_tokens=1, total_tokens=13),
        )
        recorder = ProviderCallTelemetryRecorder(
            provider_key="test",
            protocol="openai_responses",
            model=self.model,
            base_url="https://example.test/v1",
            stream=False,
            tools=kwargs.get("tools"),
        )
        return recorder.run(lambda: response)


class _ContinuationClient:
    cache_keepalive_transport = "response_continuation"


class _LegacyClient:
    model = "legacy-model"

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
        temperature: float,
    ) -> LLMResponse:
        raise AssertionError("a bounded keepalive must not fall back to an unbounded call")


class _FailingClient:
    model = "failing-model"
    base_url = "https://failing.example/v1"
    provider_key = "failing"
    protocol = "test"

    def __init__(self) -> None:
        self.calls = 0
        self.second_failure = threading.Event()

    def chat(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls >= 2:
            self.second_failure.set()
        raise ConnectionError("keepalive transport unavailable")


def test_keepalive_fires_only_after_threshold_while_sync_child_waits() -> None:
    pinged = threading.Event()
    calls: list[CacheKeepaliveRequest] = []

    def _send(request: CacheKeepaliveRequest, _stop: threading.Event) -> bool:
        calls.append(request)
        pinged.set()
        return True

    controller = ParentCacheKeepalive(
        enabled=True,
        idle_threshold_s=0.04,
        send_ping=_send,
        deadline=None,
    )
    controller.note_parent_request(
        messages=[{"role": "system", "content": "stable"}],
        tools=[{"type": "function", "function": {"name": "x"}}],
        tool_choice=None,
    )

    with controller.synchronous_child_wait():
        assert pinged.wait(0.015) is False
        assert pinged.wait(0.3) is True

    count_after_wait = len(calls)
    time.sleep(0.06)
    assert count_after_wait == 1
    assert len(calls) == count_after_wait


def test_keepalive_refuses_continuation_transport_with_one_warning() -> None:
    pinged = threading.Event()
    store = _Store()
    session = object.__new__(AgentSession)
    session.store = store  # type: ignore[assignment]
    reason = cache_keepalive_unsupported_reason(_ContinuationClient())

    controller = ParentCacheKeepalive(
        enabled=True,
        idle_threshold_s=0.01,
        send_ping=lambda _request, _stop: pinged.set() or True,
        on_unsupported=session._on_cache_keepalive_unsupported,
        unsupported_reason=reason,
        deadline=None,
    )
    controller.note_parent_request(
        messages=[{"role": "system", "content": "stable"}],
        tools=None,
    )
    with controller.synchronous_child_wait():
        assert pinged.wait(0.03) is False

    assert reason == "response_continuation"
    assert controller.enabled is False
    assert store.events == [
        (
            "warning",
            {
                "warning": "keepalive_unsupported_transport",
                "reason": "response_continuation",
                "message": (
                    "Cache keepalive is disabled because this transport does not replay the "
                    "parent's active prompt-cache stream."
                ),
            },
        )
    ]


def test_keepalive_allows_stateless_full_request_transport() -> None:
    assert cache_keepalive_unsupported_reason(_KeepaliveClient()) is None


def test_keepalive_is_disabled_by_default() -> None:
    cfg = AppConfig()
    assert cfg.cache.keepalive_enabled is False
    assert cfg.cache.keepalive_idle_threshold_s == 240.0

    pinged = threading.Event()
    controller = ParentCacheKeepalive(
        enabled=cfg.cache.keepalive_enabled,
        idle_threshold_s=0.01,
        send_ping=lambda _request, _stop: pinged.set() or True,
        deadline=None,
    )
    controller.note_parent_request(messages=[{"role": "system", "content": "x"}], tools=None)
    with controller.synchronous_child_wait():
        assert pinged.wait(0.03) is False


def test_keepalive_is_suppressed_by_deadline_pressure() -> None:
    for phase, remaining_s in (
        (DeadlinePhase.NORMAL, 0.01),
        (DeadlinePhase.FINALIZATION_WINDOW, 10.0),
    ):
        pinged = threading.Event()
        controller = ParentCacheKeepalive(
            enabled=True,
            idle_threshold_s=0.03,
            send_ping=lambda _request, _stop, event=pinged: event.set() or True,
            deadline=_Deadline(phase=phase, remaining_s=remaining_s),  # type: ignore[arg-type]
        )
        controller.note_parent_request(
            messages=[{"role": "system", "content": "stable"}],
            tools=None,
        )
        with controller.synchronous_child_wait():
            assert pinged.wait(0.08) is False


def test_keepalive_ping_shape_usage_and_telemetry_do_not_touch_transcript() -> None:
    reset_provider_telemetry_for_tests()
    client = _KeepaliveClient()
    store = _Store()
    session = object.__new__(AgentSession)
    session.client = client  # type: ignore[assignment]
    session.store = store  # type: ignore[assignment]
    session.usage_role = "main"
    session.messages = [{"role": "system", "content": "stable"}]
    original_messages = copy.deepcopy(session.messages)
    usage_calls: list[dict[str, Any]] = []

    def _record_usage(**kwargs: Any) -> _UsageRecord:
        usage_calls.append(kwargs)
        return _UsageRecord()

    session._record_llm_usage = _record_usage  # type: ignore[method-assign]
    request = CacheKeepaliveRequest(
        messages=copy.deepcopy(session.messages),
        tools=[{"type": "function", "function": {"name": "fs_list"}}],
        tool_choice={"type": "function", "function": {"name": "fs_list"}},
    )

    assert session._send_cache_keepalive(request, threading.Event()) is True

    assert session.messages == original_messages
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["messages"] == original_messages
    assert call["tools"] == request.tools
    assert call["tool_choice"] == request.tool_choice
    assert call["stream"] is False
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 16
    assert call["cancellation_token"].is_cancelled is False
    assert usage_calls[0]["operation"] == "cache_keepalive"
    assert usage_calls[0]["role_override"] == "main:cache_keepalive"
    assert last_provider_call_summary()["operation"] == "cache_keepalive"  # type: ignore[index]
    assert store.events[-1][0] == "cache_keepalive"
    assert store.events[-1][1]["usage"]["total_tokens"] == 13


def test_keepalive_failure_for_client_without_bounded_shape_is_recorded() -> None:
    reset_provider_telemetry_for_tests()
    store = _Store()
    session = object.__new__(AgentSession)
    session.client = _LegacyClient()  # type: ignore[assignment]
    session.store = store  # type: ignore[assignment]
    session.usage_role = "main"

    assert (
        session._send_cache_keepalive(
            CacheKeepaliveRequest(
                messages=[{"role": "system", "content": "stable"}],
                tools=None,
                tool_choice=None,
            ),
            threading.Event(),
        )
        is False
    )

    assert len(store.events) == 1
    assert store.events[0][0] == "cache_keepalive"
    assert store.events[0][1]["status"] == "failed"
    assert store.events[0][1]["error_class"] == "TypeError"
    telemetry = last_provider_call_summary()
    assert telemetry is not None
    assert telemetry["operation"] == "cache_keepalive"
    assert telemetry["status_category"] == "failed"
    assert telemetry["error_type"] == "TypeError"


def test_keepalive_uses_responses_temperature_compatibility_retry() -> None:
    reset_provider_telemetry_for_tests()
    base_url = "https://api.openai.com/v1"
    model = "gpt-keepalive-temperature-test"
    omit_key = _responses_temperature_omit_key(base_url, model)
    _RESPONSES_OMIT_TEMPERATURE_MODELS.discard(omit_key)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Unsupported parameter: 'temperature' is not supported with this "
                            "model. Only the default (1) value is supported."
                        ),
                        "param": "temperature",
                        "code": "unsupported_parameter",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_keepalive",
                "model": model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url=base_url,
        api_key="test-key",
        model=model,
        transport=httpx.MockTransport(handler),
    )
    store = _Store()
    session = object.__new__(AgentSession)
    session.client = client  # type: ignore[assignment]
    session.store = store  # type: ignore[assignment]
    session.usage_role = "main"
    session._record_llm_usage = lambda **_kwargs: _UsageRecord()  # type: ignore[method-assign]

    try:
        succeeded = session._send_cache_keepalive(
            CacheKeepaliveRequest(
                messages=[{"role": "system", "content": "stable parent prefix"}],
                tools=None,
                tool_choice=None,
            ),
            threading.Event(),
        )
    finally:
        _RESPONSES_OMIT_TEMPERATURE_MODELS.discard(omit_key)

    assert succeeded is True
    assert len(requests) == 2
    assert requests[0]["temperature"] == 0.0
    assert "temperature" not in requests[1]
    assert [request["max_output_tokens"] for request in requests] == [16, 16]
    telemetry = last_provider_call_summary()
    assert telemetry is not None
    assert telemetry["operation"] == "cache_keepalive"
    assert telemetry["status_category"] == "success"


def test_keepalive_failure_telemetry_and_two_strike_self_disable() -> None:
    reset_provider_telemetry_for_tests()
    client = _FailingClient()
    store = _Store()
    session = object.__new__(AgentSession)
    session.client = client  # type: ignore[assignment]
    session.store = store  # type: ignore[assignment]
    session.usage_role = "main"
    controller = ParentCacheKeepalive(
        enabled=True,
        idle_threshold_s=0.01,
        send_ping=session._send_cache_keepalive,
        on_disabled=session._on_cache_keepalive_disabled,
        deadline=None,
    )
    controller.note_parent_request(
        messages=[{"role": "system", "content": "stable parent prefix"}],
        tools=None,
    )

    started = time.monotonic()
    with controller.synchronous_child_wait():
        assert client.second_failure.wait(0.5)
    child_wait_exit_s = time.monotonic() - started
    calls_after_disable = client.calls
    time.sleep(0.04)

    assert child_wait_exit_s < 0.2
    assert calls_after_disable == 2
    assert client.calls == 2
    assert controller.enabled is False
    failures = [
        payload
        for event_type, payload in store.events
        if event_type == "cache_keepalive" and payload.get("status") == "failed"
    ]
    assert [payload["error_class"] for payload in failures] == [
        "ConnectionError",
        "ConnectionError",
    ]
    warnings = [
        payload
        for event_type, payload in store.events
        if event_type == "warning"
        and payload.get("warning") == "cache_keepalive_disabled_after_failures"
    ]
    assert len(warnings) == 1
    assert warnings[0]["consecutive_failures"] == 2
    telemetry = provider_call_history_snapshot()
    assert len(telemetry) == 2
    assert all(item["operation"] == "cache_keepalive" for item in telemetry)
    assert all(item["status_category"] == "failed" for item in telemetry)
    assert all(item["error_type"] == "ConnectionError" for item in telemetry)


def test_keepalive_config_keys_are_settable_and_validated() -> None:
    cfg = set_config_value(AppConfig(), "cache.keepalive_enabled", "true")
    cfg = set_config_value(cfg, "cache.keepalive_idle_threshold_s", "90.5")
    assert cfg.cache.keepalive_enabled is True
    assert cfg.cache.keepalive_idle_threshold_s == 90.5

    for value in ("0", "-1", "nan", "inf", "not-a-number"):
        try:
            set_config_value(cfg, "cache.keepalive_idle_threshold_s", value)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"expected ConfigError for {value}")
