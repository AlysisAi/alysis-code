from __future__ import annotations

import errno
import logging
import random
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlsplit

from ..failure_category import is_provider_throttling_error, provider_unavailable_retry_reason
from ..provider_url import known_provider_key_from_base_url
from .types import LLMStreamNoProgressError

T = TypeVar("T")

DEFAULT_PROVIDER_CONCURRENCY_CAPS: dict[str, int] = {"qwen": 4}
DEFAULT_PROVIDER_RETRY_MAX_RETRIES = 1
DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS = 10.0
DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS = 120.0

_LOGGER = logging.getLogger(__name__)
_JITTER_RATIO = 0.25
_QWEN_CANONICAL_KEY = "qwen"
_QWEN_PROVIDER_ALIASES = frozenset(
    {
        "aliyun",
        "aliyuncs",
        "dashscope",
        "dashscopechat",
        "qwen",
        "qwen2",
        "qwen25",
        "qwen3",
        "qwen35",
        "qwenmax",
        "tongyi",
    }
)
_GENERIC_HOST_PARTS = frozenset(
    {
        "api",
        "ai",
        "chat",
        "coding",
        "compatible",
        "com",
        "cn",
        "intl",
        "mode",
        "v1",
        "v1beta",
        "www",
    }
)
_KNOWN_TRANSPORT_PROVIDER_KEYS = frozenset(
    {
        "qwen",
        "openrouter",
        "openai",
        "azure",
        "deepseek",
        "gemini",
        "mistral",
        "nvidia",
        "zai_coding_plan",
        "xai",
    }
)
_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[tuple[str, int], threading.Semaphore] = {}


@dataclass(frozen=True)
class ProviderRetrySettings:
    max_retries: int = DEFAULT_PROVIDER_RETRY_MAX_RETRIES
    base_delay_seconds: float = DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS


def canonical_provider_key(provider_key: str | None) -> str | None:
    normalized = _normalize_provider_key(provider_key)
    if not normalized:
        return None
    tokens = _provider_tokens(normalized)
    if tokens & _QWEN_PROVIDER_ALIASES:
        return _QWEN_CANONICAL_KEY
    return normalized


def best_effort_provider_key(*, base_url: str | None, model: str | None) -> str | None:
    url_key = _provider_key_from_base_url(base_url)
    if url_key in _KNOWN_TRANSPORT_PROVIDER_KEYS:
        return url_key
    model_key = _provider_key_from_model(model)
    if model_key is not None:
        return model_key
    return url_key


def resolve_provider_retry_settings(cfg: object | None) -> ProviderRetrySettings:
    return ProviderRetrySettings(
        max_retries=_coerce_non_negative_int(
            getattr(cfg, "provider_retry_max_retries", DEFAULT_PROVIDER_RETRY_MAX_RETRIES),
            default=DEFAULT_PROVIDER_RETRY_MAX_RETRIES,
        ),
        base_delay_seconds=_coerce_positive_float(
            getattr(
                cfg,
                "provider_retry_base_delay_seconds",
                DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS,
            ),
            default=DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS,
        ),
        max_delay_seconds=_coerce_positive_float(
            getattr(
                cfg,
                "provider_retry_max_delay_seconds",
                DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS,
            ),
            default=DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS,
        ),
    )


def resolve_provider_concurrency_cap(
    provider_concurrency_caps: Mapping[str, int | None] | None,
    provider_key: str | None,
) -> int | None:
    canonical_key = canonical_provider_key(provider_key)
    if canonical_key is None:
        return None
    caps = provider_concurrency_caps or DEFAULT_PROVIDER_CONCURRENCY_CAPS
    exact_found, exact_cap = _lookup_cap(caps, canonical_key)
    if exact_found:
        return exact_cap

    alias_caps: list[int] = []
    for raw_key, raw_cap in caps.items():
        if canonical_provider_key(str(raw_key)) != canonical_key:
            continue
        cap = _coerce_cap(raw_cap)
        if cap is not None:
            alias_caps.append(cap)
    if alias_caps:
        return min(alias_caps)
    return None


def run_provider_limited_call(
    *,
    call: Callable[[], T],
    provider_key: str | None,
    provider_concurrency_caps: Mapping[str, int | None] | None = None,
    retry_settings: ProviderRetrySettings | None = None,
    operation: str,
    sleep_fn: Callable[[float], None] | None = None,
    random_fn: Callable[[], float] | None = None,
    on_retry: Callable[[int, str, float], None] | None = None,
    on_retry_event: Callable[[dict[str, object]], None] | None = None,
    retry_deadline_allows: Callable[[float], bool] | None = None,
    retry_wall_clock_cap_seconds: float | None = None,
    clock_fn: Callable[[], float] | None = None,
) -> T:
    settings = retry_settings or ProviderRetrySettings()
    canonical_key = canonical_provider_key(provider_key)
    cap = resolve_provider_concurrency_cap(provider_concurrency_caps, canonical_key)
    semaphore = _semaphore_for(canonical_key, cap) if canonical_key and cap else None
    sleep = sleep_fn or time.sleep
    jitter = random_fn or random.random
    clock = clock_fn or time.monotonic
    retry_started_at = clock()
    retries_used = 0

    while True:
        if semaphore is not None:
            semaphore.acquire()
        try:
            return call()
        except Exception as exc:
            retry_reason = _provider_retry_reason(exc)
            if (
                retry_reason is None
                or _is_provider_call_non_retryable(exc)
                or retries_used >= settings.max_retries
            ):
                raise
            wait_seconds = _retry_delay_seconds(settings, retries_used, jitter)
            if _retry_wall_clock_cap_blocks(
                retry_reason=retry_reason,
                retry_started_at=retry_started_at,
                wait_seconds=wait_seconds,
                cap_seconds=retry_wall_clock_cap_seconds,
                clock=clock,
            ):
                _LOGGER.info(
                    "provider_retry_wall_clock_cap_blocked",
                    extra={
                        "operation": operation,
                        "provider_key": canonical_key,
                        "retry_attempt": retries_used + 1,
                        "wait_seconds": wait_seconds,
                        "retry_reason": retry_reason,
                    },
                )
                raise
            if retry_deadline_allows is not None and not retry_deadline_allows(wait_seconds):
                _LOGGER.info(
                    "provider_retry_deadline_blocked",
                    extra={
                        "operation": operation,
                        "provider_key": canonical_key,
                        "retry_attempt": retries_used + 1,
                        "wait_seconds": wait_seconds,
                        "retry_reason": retry_reason,
                    },
                )
                raise
            if on_retry is not None:
                try:
                    on_retry(retries_used + 1, retry_reason, wait_seconds)
                except Exception:  # noqa: BLE001 - observers must not change retry behavior.
                    _LOGGER.debug("provider_retry_observer_failed", exc_info=True)
            if on_retry_event is not None:
                try:
                    on_retry_event(
                        {
                            "provider": canonical_key or "unknown",
                            "attempt": retries_used + 2,
                            "reason": retry_reason,
                            "elapsed_ms": max(
                                0,
                                int((clock() - retry_started_at) * 1000),
                            ),
                        }
                    )
                except Exception:  # noqa: BLE001 - observers cannot change retries.
                    _LOGGER.debug("provider_retry_event_observer_failed", exc_info=True)
            _LOGGER.info(
                f"{retry_reason}, retrying",
                extra={
                    "operation": operation,
                    "provider_key": canonical_key,
                    "retry_attempt": retries_used + 1,
                    "wait_seconds": wait_seconds,
                },
            )
        finally:
            if semaphore is not None:
                semaphore.release()
        retries_used += 1
        sleep(wait_seconds)


def _retry_wall_clock_cap_blocks(
    *,
    retry_reason: str,
    retry_started_at: float,
    wait_seconds: float,
    cap_seconds: float | None,
    clock: Callable[[], float],
) -> bool:
    if (
        retry_reason not in {"provider_unavailable", "provider_stream_truncated"}
        or cap_seconds is None
    ):
        return False
    try:
        cap = float(cap_seconds)
    except (TypeError, ValueError):
        return False
    if cap <= 0:
        return True
    elapsed = max(0.0, float(clock()) - float(retry_started_at))
    return elapsed + max(0.0, float(wait_seconds)) > cap


def _provider_retry_reason(exc: Exception) -> str | None:
    if isinstance(exc, LLMStreamNoProgressError):
        return "provider_no_progress"
    if is_provider_throttling_error(exc):
        return "provider_throttled"
    transport_reason = _transient_transport_retry_reason(exc)
    if transport_reason is not None:
        return transport_reason
    return provider_unavailable_retry_reason(exc)


_TRANSIENT_TRANSPORT_ERRNOS = {
    value
    for value in (
        getattr(socket, "EAI_AGAIN", None),
        getattr(socket, "EAI_FAIL", None),
        getattr(socket, "EAI_NONAME", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "ETIMEDOUT", None),
    )
    if value is not None
}
_TRANSIENT_TRANSPORT_MARKERS = (
    "connection aborted",
    "connection refused",
    "connection reset",
    "getaddrinfo failed",
    "handshake operation timed out",
    "name or service not known",
    "name resolution",
    "network is unreachable",
    "nodename nor servname",
    "temporary failure in name resolution",
    "timed out",
)


def _transient_transport_retry_reason(exc: BaseException) -> str | None:
    for item in _iter_exception_chain(exc):
        if isinstance(item, (socket.timeout, TimeoutError)):
            return "provider_unavailable"
        if isinstance(item, ssl.SSLError) and _has_transient_transport_marker(item):
            return "provider_unavailable"
        if isinstance(
            item,
            (ConnectionAbortedError, ConnectionRefusedError, ConnectionResetError),
        ):
            return "provider_unavailable"
        if isinstance(item, OSError):
            if getattr(item, "errno", None) in _TRANSIENT_TRANSPORT_ERRNOS:
                return "provider_unavailable"
            if _has_transient_transport_marker(item):
                return "provider_unavailable"
    return None


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        cause = current.__cause__
        context = current.__context__
        current = cause if cause is not None else context
    return chain


def _has_transient_transport_marker(exc: BaseException) -> bool:
    message = str(exc or "").casefold()
    return any(marker in message for marker in _TRANSIENT_TRANSPORT_MARKERS)


PROVIDER_NON_RETRYABLE_ATTR = "_provider_call_non_retryable"


def mark_provider_call_non_retryable(exc: BaseException) -> None:
    """Tag an exception so :func:`run_provider_limited_call` will not retry it.

    Some failures match the retryable heuristics by message (e.g. an httpx read
    timeout contains "timed out") yet an immediate retry with the same budget is
    pointless — it will fail the same way and only double the dead air. A caller
    that knows a failure is not worth retrying marks it here so the retry loop
    skips it while still retrying genuinely transient failures.
    """
    try:
        setattr(exc, PROVIDER_NON_RETRYABLE_ATTR, True)
    except Exception:  # noqa: BLE001 - never let tagging crash the caller
        pass


def _is_provider_call_non_retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, PROVIDER_NON_RETRYABLE_ATTR, False))


def single_retry_is_worthwhile(exc: BaseException) -> bool:
    """Whether one more attempt at the same call could plausibly succeed.

    Only transport-class failures qualify: timeouts, resets, refused
    connections, and transient SSL/OS errors. A rejection — bad credentials,
    exhausted quota, a malformed request — fails identically on the second
    attempt and only spends another round trip. Failures the provider layer has
    already given up on are never retried again on top of that.

    Exposed for callers outside this module that run their own bounded retry,
    so the transient classification stays defined in exactly one place.
    """

    if _is_provider_call_non_retryable(exc):
        return False
    return _transient_transport_retry_reason(exc) is not None


def reset_provider_limit_state_for_tests() -> None:
    with _SEMAPHORE_LOCK:
        _SEMAPHORES.clear()


def qwen_provider_aliases() -> tuple[str, ...]:
    return tuple(sorted(_QWEN_PROVIDER_ALIASES))


def _semaphore_for(provider_key: str, cap: int) -> threading.Semaphore:
    key = (provider_key, cap)
    with _SEMAPHORE_LOCK:
        semaphore = _SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.Semaphore(cap)
            _SEMAPHORES[key] = semaphore
        return semaphore


def _retry_delay_seconds(
    settings: ProviderRetrySettings,
    retry_index: int,
    random_fn: Callable[[], float],
) -> float:
    base = max(settings.base_delay_seconds, 0.0)
    max_delay = max(settings.max_delay_seconds, base)
    raw_delay = min(max_delay, base * (2**retry_index))
    jitter_sample = min(max(float(random_fn()), 0.0), 1.0)
    jitter_offset = (jitter_sample - 0.5) * 2.0 * _JITTER_RATIO * raw_delay
    return max(0.0, min(max_delay, raw_delay + jitter_offset))


def _lookup_cap(
    caps: Mapping[str, int | None],
    key: str,
) -> tuple[bool, int | None]:
    for raw_key, raw_cap in caps.items():
        if _normalize_provider_key(str(raw_key)) == key:
            return True, _coerce_cap(raw_cap)
    return False, None


def _coerce_cap(raw: int | None) -> int | None:
    try:
        cap = int(raw if raw is not None else 0)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def _coerce_non_negative_int(raw: object, *, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _coerce_positive_float(raw: object, *, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _provider_key_from_model(model: str | None) -> str | None:
    raw_model = str(model or "").strip()
    normalized = _normalize_provider_key(raw_model)
    if not normalized:
        return None
    canonical = canonical_provider_key(normalized)
    if canonical is not None and canonical != normalized:
        return canonical
    if "/" in raw_model:
        prefix = raw_model.split("/", 1)[0]
        return canonical_provider_key(prefix) or _normalize_provider_key(prefix)
    return None


def _provider_key_from_base_url(base_url: str | None) -> str | None:
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        hostname = (parts.hostname or "").rstrip(".").casefold()
    except ValueError:
        return None
    if not hostname:
        return None
    known_provider = known_provider_key_from_base_url(raw)
    if known_provider:
        return known_provider
    canonical = canonical_provider_key(hostname)
    if canonical is not None and canonical != hostname:
        return canonical
    host_parts = [part for part in hostname.replace("-", ".").split(".") if part]
    for part in host_parts:
        canonical_part = canonical_provider_key(part)
        if canonical_part is not None and canonical_part != part:
            return canonical_part
    for part in reversed(host_parts):
        if part not in _GENERIC_HOST_PARTS:
            return canonical_provider_key(part) or part
    return hostname


def _normalize_provider_key(provider_key: str | None) -> str | None:
    normalized = str(provider_key or "").strip().casefold()
    if not normalized:
        return None
    return "".join(char if char.isalnum() else "_" for char in normalized).strip("_") or None


def _provider_tokens(provider_key: str) -> set[str]:
    ordered_tokens = [
        token
        for token in provider_key.replace("-", "_").replace("/", "_").replace(".", "_").split("_")
        if token
    ]
    tokens = set(ordered_tokens)
    compact = "".join(ordered_tokens)
    if compact:
        tokens.add(compact)
    return tokens
