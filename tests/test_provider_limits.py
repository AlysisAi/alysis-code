from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import alysis_code.llm.openai_compat as openai_compat_mod
import alysis_code.llm.provider_limits as provider_limits_mod
import alysis_code.model_registry as model_registry_mod
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMError, OpenAICompatClient
from alysis_code.llm.provider_limits import (
    ProviderRetrySettings,
    best_effort_provider_key,
    canonical_provider_key,
    mark_provider_call_non_retryable,
    qwen_provider_aliases,
    reset_provider_limit_state_for_tests,
    resolve_provider_concurrency_cap,
    run_provider_limited_call,
)
from alysis_code.model_registry import resolve_model_provider_key
from alysis_code.tools.web_search_dashscope import dashscope_chat_search


def teardown_function() -> None:
    reset_provider_limit_state_for_tests()


class _Provider429(RuntimeError):
    status_code = 429

    def __str__(self) -> str:
        return "rate limit exceeded"


def _chat_ok() -> dict[str, object]:
    return {"choices": [{"message": {"content": "ok"}}]}


def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_route_identity_prevents_model_name_from_borrowing_qwen_cap() -> None:
    cfg = AppConfig(base_url="https://api.deepseek.com", model="deepseek-v4-pro")

    provider_key = resolve_model_provider_key(
        cfg=cfg,
        model_name="qwen3.5-plus",
        base_url=cfg.base_url,
    )
    canonical_key = canonical_provider_key(provider_key)
    cap = resolve_provider_concurrency_cap(cfg.provider_concurrency_caps, canonical_key)

    assert provider_key == "deepseek"
    assert canonical_key == "deepseek"
    assert cap is None


def test_qwen_aliases_fold_to_single_canonical_key() -> None:
    aliases = qwen_provider_aliases()

    assert "dashscope" in aliases
    assert "qwen3" in aliases
    assert canonical_provider_key("dashscope_chat") == "qwen"
    assert (
        best_effort_provider_key(
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            model="qwen3.5-plus",
        )
        == "qwen"
    )


@pytest.mark.parametrize(
    ("base_url", "model", "expected_provider_key"),
    [
        ("https://dashscope-us.aliyuncs.com/compatible-mode/v1", "qwen3.5-plus", "qwen"),
        ("https://openrouter.ai/api/v1", "openai/gpt-5", "openrouter"),
        ("https://api.openai.com/v1", "gpt-5", "openai"),
        ("https://api.deepseek.com/v1", "deepseek-chat", "deepseek"),
        ("https://api.z.ai/api/coding/paas/v4", "glm-5.3", "zai_coding_plan"),
        (
            "https://integrate.api.nvidia.com/v1",
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-3.1-pro-preview",
            "gemini",
        ),
        ("https://api.mistral.ai/v1", "mistral-large-latest", "mistral"),
        ("https://api.x.ai/v1", "grok-4", "xai"),
        ("https://api.kimi.com/coding/v1", "k3", "kimi-code"),
        # The Alysis Code hosted proxy (llm Edge Function) forwards to DeepSeek.
        (
            "https://vzigujbcjjmpntxhmyvr.supabase.co/functions/v1/llm/v1",
            "deepseek-v4-flash",
            "deepseek",
        ),
    ],
)
def test_known_endpoint_provider_keys(
    base_url: str,
    model: str,
    expected_provider_key: str,
) -> None:
    assert best_effort_provider_key(base_url=base_url, model=model) == expected_provider_key


def test_resolve_model_provider_key_keeps_openrouter_transport_provider() -> None:
    cfg = AppConfig(
        base_url="https://openrouter.ai/api/v1",
        model="qwen3.5-plus",
    )

    provider_key = resolve_model_provider_key(
        cfg=cfg,
        model_name="qwen3.5-plus",
        base_url=cfg.base_url,
    )

    assert provider_key == "openrouter"


def test_zai_general_api_is_not_misclassified_as_coding_plan() -> None:
    assert (
        best_effort_provider_key(
            base_url="https://api.z.ai/api/paas/v4",
            model="glm-5.3",
        )
        != "zai_coding_plan"
    )
    assert (
        best_effort_provider_key(
            base_url="https://api.z.ai/api/coding/paas/v40",
            model="glm-5.3",
        )
        != "zai_coding_plan"
    )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://integrate.api.nvidia.com/v1", "nvidia"),
        ("https://api.z.ai/api/coding/paas/v4", "zai_coding_plan"),
        (
            "https://api.z.ai/api/coding/paas/v4/chat/completions",
            "zai_coding_plan",
        ),
        ("https://api.z.ai/api/paas/v4", None),
        ("https://api.z.ai/", None),
    ],
)
def test_provider_url_classification_agrees_across_all_call_sites(
    base_url: str,
    expected: str | None,
) -> None:
    results = (
        openai_compat_mod._provider_key_from_base_url(base_url),
        provider_limits_mod._provider_key_from_base_url(base_url),
        model_registry_mod._provider_key_from_base_url(base_url),
    )

    if expected is None:
        assert all(result != "zai_coding_plan" for result in results)
    else:
        assert results == (expected, expected, expected)


def test_concurrency_cap_respected_for_parallel_requests() -> None:
    caps = {"qwen": 2}
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()
    release = threading.Event()
    first_two_entered = threading.Event()
    entered = 0

    def call() -> str:
        nonlocal entered, in_flight, max_in_flight
        with lock:
            entered += 1
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if entered == 2:
                first_two_entered.set()
        release.wait(timeout=2.0)
        with lock:
            in_flight -= 1
        return "ok"

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(
                run_provider_limited_call,
                call=call,
                provider_key="qwen",
                provider_concurrency_caps=caps,
                retry_settings=ProviderRetrySettings(max_retries=0),
                operation="test",
            )
            for _ in range(5)
        ]
        assert first_two_entered.wait(timeout=1.0)
        time.sleep(0.05)
        assert max_in_flight == 2
        release.set()
        assert [future.result(timeout=1.0) for future in futures] == ["ok"] * 5


def test_cross_provider_requests_do_not_share_qwen_cap() -> None:
    caps = {"qwen": 1}
    qwen_entered = threading.Event()
    release_qwen = threading.Event()

    def qwen_call() -> str:
        qwen_entered.set()
        release_qwen.wait(timeout=2.0)
        return "qwen"

    def openai_call() -> str:
        return "openai"

    with ThreadPoolExecutor(max_workers=2) as pool:
        qwen_future = pool.submit(
            run_provider_limited_call,
            call=qwen_call,
            provider_key="qwen",
            provider_concurrency_caps=caps,
            retry_settings=ProviderRetrySettings(max_retries=0),
            operation="qwen",
        )
        assert qwen_entered.wait(timeout=1.0)
        openai_future = pool.submit(
            run_provider_limited_call,
            call=openai_call,
            provider_key="openai",
            provider_concurrency_caps=caps,
            retry_settings=ProviderRetrySettings(max_retries=0),
            operation="openai",
        )
        assert openai_future.result(timeout=0.5) == "openai"
        release_qwen.set()
        assert qwen_future.result(timeout=1.0) == "qwen"


def test_backoff_success_after_transient_provider_throttling() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _Provider429()
        return "ok"

    result = run_provider_limited_call(
        call=call,
        provider_key="qwen",
        provider_concurrency_caps={"qwen": 0},
        retry_settings=ProviderRetrySettings(
            max_retries=3,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
        ),
        operation="test_backoff",
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )

    assert result == "ok"
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


def test_backoff_exhaustion_reraises_provider_throttling_error() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        raise _Provider429()

    with pytest.raises(_Provider429):
        run_provider_limited_call(
            call=call,
            provider_key="qwen",
            provider_concurrency_caps={"qwen": 0},
            retry_settings=ProviderRetrySettings(
                max_retries=2,
                base_delay_seconds=1.0,
                max_delay_seconds=30.0,
            ),
            operation="test_exhaustion",
            sleep_fn=sleeps.append,
            random_fn=lambda: 0.5,
        )

    assert attempts == 3
    assert sleeps == [1.0, 2.0]


def test_backoff_success_after_transient_provider_unavailable_error() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LLMError("LLM request failed: [Errno -3] Temporary failure in name resolution")
        if attempts == 2:
            raise LLMError("LLM request failed: The read operation timed out")
        return "ok"

    result = run_provider_limited_call(
        call=call,
        provider_key="deepseek",
        provider_concurrency_caps={},
        retry_settings=ProviderRetrySettings(
            max_retries=3,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
        ),
        operation="test_unavailable",
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )

    assert result == "ok"
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


def test_default_provider_retry_schedule_caps_at_two_attempts() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        raise LLMError("LLM error 503: service unavailable")

    with pytest.raises(LLMError, match="503"):
        run_provider_limited_call(
            call=call,
            provider_key="openai",
            provider_concurrency_caps={},
            operation="default_retry_schedule",
            sleep_fn=sleeps.append,
            random_fn=lambda: 0.5,
        )

    assert attempts == 2
    assert sleeps == [10.0]


def test_retry_event_observer_receives_content_free_live_payload() -> None:
    attempts = 0
    events: list[dict[str, object]] = []
    clock_values = iter((10.0, 10.25))

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LLMError("LLM error 503: service unavailable")
        return "ok"

    assert (
        run_provider_limited_call(
            call=call,
            provider_key="deepseek",
            retry_settings=ProviderRetrySettings(max_retries=1),
            operation="chat",
            sleep_fn=lambda _seconds: None,
            random_fn=lambda: 0.5,
            clock_fn=lambda: next(clock_values),
            on_retry_event=events.append,
        )
        == "ok"
    )
    assert events == [
        {
            "provider": "deepseek",
            "attempt": 2,
            "reason": "provider_unavailable",
            "elapsed_ms": 250,
        }
    ]


def test_provider_unavailable_retry_wall_clock_cap_blocks_backoff() -> None:
    attempts = 0
    sleeps: list[float] = []
    clock_values = iter((0.0, 0.25))

    def clock() -> float:
        return next(clock_values)

    def call() -> str:
        nonlocal attempts
        attempts += 1
        raise LLMError("LLM request failed: The read operation timed out")

    with pytest.raises(LLMError, match="timed out"):
        run_provider_limited_call(
            call=call,
            provider_key=None,
            provider_concurrency_caps={},
            retry_settings=ProviderRetrySettings(
                max_retries=3,
                base_delay_seconds=1.0,
                max_delay_seconds=30.0,
            ),
            operation="test_retry_cap",
            sleep_fn=sleeps.append,
            random_fn=lambda: 0.5,
            retry_wall_clock_cap_seconds=0.5,
            clock_fn=clock,
        )

    assert attempts == 1
    assert sleeps == []


def test_marked_non_retryable_error_is_not_retried_despite_retryable_message() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        # Message would otherwise be classified as provider_unavailable (retryable),
        # but the marker must short-circuit the retry loop.
        error = LLMError("LLM request failed: The read operation timed out")
        mark_provider_call_non_retryable(error)
        raise error

    with pytest.raises(LLMError):
        run_provider_limited_call(
            call=call,
            provider_key="openrouter",
            provider_concurrency_caps={},
            retry_settings=ProviderRetrySettings(
                max_retries=5,
                base_delay_seconds=1.0,
                max_delay_seconds=30.0,
            ),
            operation="test_non_retryable",
            sleep_fn=sleeps.append,
            random_fn=lambda: 0.5,
        )

    assert attempts == 1
    assert sleeps == []


def test_typed_remote_protocol_truncation_is_retryable() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )
        return "ok"

    result = run_provider_limited_call(
        call=call,
        provider_key="openrouter",
        provider_concurrency_caps={},
        retry_settings=ProviderRetrySettings(
            max_retries=1,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
        ),
        operation="typed_truncation",
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )

    assert result == "ok"
    assert attempts == 2
    assert sleeps == [1.0]


def test_wrapped_transport_truncation_stays_retryable_through_cause_chain() -> None:
    attempts = 0
    reasons: list[str] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            cause = httpx.RemoteProtocolError("incomplete chunked read")
            raise LLMError("LLM request failed: stream interrupted") from cause
        return "ok"

    result = run_provider_limited_call(
        call=call,
        provider_key="openrouter",
        provider_concurrency_caps={},
        retry_settings=ProviderRetrySettings(max_retries=1),
        operation="wrapped_truncation",
        sleep_fn=lambda _seconds: None,
        random_fn=lambda: 0.5,
        on_retry=lambda _attempt, reason, _seconds: reasons.append(reason),
    )

    assert result == "ok"
    assert attempts == 2
    assert reasons == ["provider_stream_truncated"]


def test_incomplete_chunked_body_message_is_transient_but_auth_is_not() -> None:
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LLMError(
                "LLM request failed: peer closed connection without sending complete "
                "message body (incomplete chunked read)"
            )
        return "ok"

    assert (
        run_provider_limited_call(
            call=call,
            provider_key="openrouter",
            provider_concurrency_caps={},
            retry_settings=ProviderRetrySettings(max_retries=1),
            operation="message_truncation",
            sleep_fn=lambda _seconds: None,
            random_fn=lambda: 0.5,
        )
        == "ok"
    )

    auth_attempts = 0

    def auth_call() -> str:
        nonlocal auth_attempts
        auth_attempts += 1
        raise LLMError("LLM error 401: invalid api key")

    with pytest.raises(LLMError, match="401"):
        run_provider_limited_call(
            call=auth_call,
            provider_key="openrouter",
            provider_concurrency_caps={},
            retry_settings=ProviderRetrySettings(max_retries=3),
            operation="auth",
            sleep_fn=lambda _seconds: (_ for _ in ()).throw(
                AssertionError("auth errors must not retry")
            ),
        )
    assert auth_attempts == 1


def test_deadline_callback_prevents_unsafe_provider_retry() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        raise LLMError("LLM request failed: The read operation timed out")

    with pytest.raises(LLMError, match="timed out"):
        run_provider_limited_call(
            call=call,
            provider_key="deepseek",
            provider_concurrency_caps={},
            retry_settings=ProviderRetrySettings(max_retries=3),
            operation="deadline_blocked",
            sleep_fn=sleeps.append,
            random_fn=lambda: 0.5,
            retry_deadline_allows=lambda _wait_seconds: False,
        )

    assert attempts == 1
    assert sleeps == []


def test_openai_compat_retries_429_rate_limit_and_500() -> None:
    attempts = 0
    sleeps: list[float] = []

    def retry_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return httpx.Response(429, text="rate limit exceeded")
        return httpx.Response(200, json=_chat_ok())

    client = OpenAICompatClient(
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="k",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(retry_handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
        provider_sleep_fn=sleeps.append,
        provider_random_fn=lambda: 0.5,
    )

    assert client.chat(messages=[{"role": "user", "content": "hi"}]).content == "ok"
    assert attempts == 3
    assert sleeps == [10.0, 20.0]

    server_error_attempts = 0
    server_error_sleeps: list[float] = []

    def server_error_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal server_error_attempts
        server_error_attempts += 1
        if server_error_attempts <= 2:
            return httpx.Response(500, text="internal error")
        return httpx.Response(200, json=_chat_ok())

    server_error_client = OpenAICompatClient(
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key="k",
        model="qwen3.5-plus",
        transport=httpx.MockTransport(server_error_handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=2,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
        ),
        provider_sleep_fn=server_error_sleeps.append,
        provider_random_fn=lambda: 0.5,
    )

    assert server_error_client.chat(messages=[{"role": "user", "content": "hi"}]).content == "ok"
    assert server_error_attempts == 3
    assert server_error_sleeps == [1.0, 2.0]


def test_dashscope_web_search_shares_qwen_semaphore_with_planner_call() -> None:
    caps = {"qwen": 1}
    planner_entered = threading.Event()
    release_planner = threading.Event()
    search_http_started = threading.Event()

    def planner_call() -> str:
        planner_entered.set()
        release_planner.wait(timeout=2.0)
        return "planner"

    def handler(_request: httpx.Request) -> httpx.Response:
        search_http_started.set()
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_dashscope_1",
                "model": "qwen3.5-plus",
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"answer":"ok","sources":'
                                '[{"title":"Docs","url":"https://example.com/docs"}]}'
                            )
                        }
                    }
                ],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        planner_future = pool.submit(
            run_provider_limited_call,
            call=planner_call,
            provider_key="qwen",
            provider_concurrency_caps=caps,
            retry_settings=ProviderRetrySettings(max_retries=0),
            operation="planner",
        )
        assert planner_entered.wait(timeout=1.0)
        search_future = pool.submit(
            dashscope_chat_search,
            query="docs",
            base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
            api_key="k",
            model="qwen3.5-plus",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
            provider_concurrency_caps=caps,
            provider_retry_settings=ProviderRetrySettings(max_retries=0),
        )
        time.sleep(0.05)
        assert not search_http_started.is_set()
        release_planner.set()
        assert planner_future.result(timeout=1.0) == "planner"
        assert search_future.result(timeout=1.0)["answer"] == "ok"
        assert search_http_started.is_set()


def test_provider_cap_state_is_global_across_worker_threads() -> None:
    caps = {"dashscope": 1}
    max_in_flight = 0
    in_flight = 0
    lock = threading.Lock()

    def worker_call() -> str:
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.02)
        with lock:
            in_flight -= 1
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _idx: run_provider_limited_call(
                    call=worker_call,
                    provider_key="qwen",
                    provider_concurrency_caps=caps,
                    retry_settings=ProviderRetrySettings(max_retries=0),
                    operation="worker",
                ),
                range(2),
            )
        )

    assert results == ["ok", "ok"]
    assert max_in_flight == 1
