"""Best-effort prompt-cache affinity while a parent waits on synchronous children."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any

from ..execution_deadline import DeadlinePhase, ExecutionDeadline


def cache_keepalive_unsupported_reason(client: Any) -> str | None:
    """Return why replay keepalive cannot preserve this client's cache stream."""
    if getattr(client, "provider_auth", None) is not None:
        return "subscription_gateway"
    transport = str(getattr(client, "cache_keepalive_transport", "") or "").strip()
    if transport == "response_continuation":
        return transport
    return None


@dataclass(frozen=True)
class CacheKeepaliveRequest:
    """Immutable copy of the last parent request prefix sent to the provider."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    tool_choice: Any | None


class ParentCacheKeepalive:
    """Refresh an idle parent prefix without changing its durable transcript."""

    def __init__(
        self,
        *,
        enabled: bool,
        idle_threshold_s: float,
        send_ping: Callable[[CacheKeepaliveRequest, threading.Event], bool],
        on_disabled: Callable[[int], None] | None = None,
        on_unsupported: Callable[[str], None] | None = None,
        unsupported_reason: str | None = None,
        deadline: ExecutionDeadline | None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        requested_enabled = bool(enabled)
        normalized_unsupported_reason = str(unsupported_reason or "").strip()
        self.enabled = requested_enabled and not normalized_unsupported_reason
        self.idle_threshold_s = max(0.001, float(idle_threshold_s))
        self._send_ping = send_ping
        self._on_disabled = on_disabled
        self._deadline = deadline
        self._clock = clock
        self._lock = threading.RLock()
        self._snapshot: CacheKeepaliveRequest | None = None
        self._last_parent_request_at: float | None = None
        self._last_ping_at: float | None = None
        self._wait_depth = 0
        self._cancelled: Callable[[], bool] | None = None
        self._stop_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._consecutive_failures = 0
        self._failure_disable_reported = False
        if requested_enabled and normalized_unsupported_reason and on_unsupported is not None:
            try:
                on_unsupported(normalized_unsupported_reason)
            except Exception:  # noqa: BLE001 - warning emission is best-effort
                pass

    def note_parent_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any | None = None,
    ) -> None:
        """Capture the exact prefix inputs before a real parent request is sent."""
        if not self.enabled:
            return
        with self._lock:
            self._snapshot = CacheKeepaliveRequest(
                messages=copy.deepcopy(messages),
                tools=copy.deepcopy(tools),
                tool_choice=copy.deepcopy(tool_choice),
            )
            self._last_parent_request_at = self._clock()
            self._last_ping_at = None

    def forget_parent_request(self) -> None:
        """Drop a snapshot that must not be retained or replayed."""
        with self._lock:
            self._snapshot = None
            self._last_parent_request_at = None
            self._last_ping_at = None

    @contextmanager
    def synchronous_child_wait(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[None]:
        """Mark time in which the parent is blocked on synchronous child work."""
        self._begin_wait(cancelled=cancelled)
        try:
            yield
        finally:
            self._end_wait()

    def _begin_wait(self, *, cancelled: Callable[[], bool] | None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._wait_depth += 1
            self._cancelled = cancelled
            if self._wait_depth != 1 or self._snapshot is None:
                return
            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._run,
                args=(stop_event,),
                name="parent-cache-keepalive",
                daemon=True,
            )
            self._stop_event = stop_event
            self._worker = worker
            worker.start()

    def _end_wait(self) -> None:
        with self._lock:
            self._wait_depth = max(0, self._wait_depth - 1)
            if self._wait_depth != 0:
                return
            self._cancelled = None
            if self._stop_event is not None:
                self._stop_event.set()
            self._stop_event = None
            self._worker = None
        # Never join here: a provider that ignores cooperative cancellation must
        # not add latency to the child result the parent is waiting for.

    def close(self) -> None:
        """Stop refreshes during session teardown."""
        worker: threading.Thread | None = None
        with self._lock:
            self._wait_depth = 0
            self._cancelled = None
            if self._stop_event is not None:
                self._stop_event.set()
            worker = self._worker
            self._stop_event = None
            self._worker = None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)

    def _run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            with self._lock:
                anchor = self._last_ping_at or self._last_parent_request_at
                active = self._wait_depth > 0
            if not active or anchor is None:
                return
            wait_s = max(0.0, anchor + self.idle_threshold_s - self._clock())
            if stop_event.wait(wait_s):
                return
            if not self._send_if_due(stop_event=stop_event):
                return

    def _send_if_due(self, *, stop_event: threading.Event) -> bool:
        """Send one due ping; return whether the periodic loop may continue."""
        with self._lock:
            snapshot = self._snapshot
            anchor = self._last_ping_at or self._last_parent_request_at
            cancelled = self._cancelled
            if (
                self._wait_depth <= 0
                or snapshot is None
                or anchor is None
                or self._clock() < anchor + self.idle_threshold_s
            ):
                return True
            if stop_event.is_set() or (cancelled is not None and cancelled()):
                return False
            if not self._deadline_allows_ping():
                return False
            # Reserve the interval before sending so a provider error cannot create
            # a tight retry loop. The next attempt is never earlier than one full
            # configured threshold.
            self._last_ping_at = self._clock()
            request = copy.deepcopy(snapshot)
        try:
            succeeded = bool(self._send_ping(request, stop_event))
        except Exception:  # noqa: BLE001 - affinity can never crash child work
            succeeded = False
        disable_callback: Callable[[int], None] | None = None
        failures = 0
        with self._lock:
            if succeeded:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
            failures = self._consecutive_failures
            if failures >= 2:
                self.enabled = False
                stop_event.set()
                if not self._failure_disable_reported:
                    self._failure_disable_reported = True
                    disable_callback = self._on_disabled
        if disable_callback is not None:
            try:
                disable_callback(failures)
            except Exception:  # noqa: BLE001 - warning emission is best-effort
                pass
        return failures < 2 and not stop_event.is_set()

    def _deadline_allows_ping(self) -> bool:
        deadline = self._deadline
        if deadline is None:
            return True
        phase = deadline.phase()
        if phase in {DeadlinePhase.FINALIZATION_WINDOW, DeadlinePhase.EXHAUSTED}:
            return False
        remaining = deadline.remaining_seconds()
        return remaining is None or remaining >= self.idle_threshold_s
