"""Bounded, concurrent WebSocket transport for Chromium DevTools Protocol.

The managed browser launches Chromium's DevTools endpoint on loopback.  This
module is the production transport for that endpoint.  It deliberately owns a
single reader thread because CDP replies and events share one WebSocket and may
arrive in any order.

Navigation installs a persistent Fetch-domain guard at both request and
response stages. No HTTP(S) request is continued until the caller's URL policy
accepts its current URL, including requests started after page load by script
or an input action. Malformed protocol traffic, an unknown response id, queue
overflow during interception, timeout, and cancellation all fail closed.
"""

from __future__ import annotations

import ipaddress
import json
import math
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias
from urllib.parse import urlsplit

from .managed_browser import (
    BrowserCancelledError,
    BrowserDependencyError,
    BrowserError,
    BrowserLaunchError,
    BrowserLimitError,
    BrowserSecurityError,
    BrowserTimeoutError,
    BrowserValidationError,
    CancelCheck,
    CdpTransport,
    CdpTransportFactory,
)

__all__ = [
    "CdpCommandError",
    "WebSocketCdpTransport",
    "WebSocketCdpTransportFactory",
    "validate_cdp_websocket_url",
]

_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_MAX_EVENT_BYTES = 16 * 1024 * 1024
_MAX_EVENT_COUNT = 512
_MAX_EVENT_DRAIN = 2_000
_MAX_METHOD_CHARS = 256
_MAX_SESSION_ID_CHARS = 256
_MAX_RESPONSE_ID = (1 << 53) - 1
_MAX_PENDING_CALLS = 256
_READER_POLL_SECONDS = 0.2
_WAIT_POLL_SECONDS = 0.05
_DEVTOOLS_PATH_RE = re.compile(r"^/devtools/browser/[A-Za-z0-9._-]{1,160}$")
_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,255}$")


class CdpCommandError(BrowserError):
    """A CDP command returned an error object.

    Chromium's error text is intentionally not reflected because it may contain
    page-controlled data.  ``code`` is retained when it is a bounded integer.
    """

    def __init__(self, method: str, code: int | None = None) -> None:
        self.method = method
        self.code = code
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"Browser CDP command failed: {method}{suffix}.")


class _WebSocketConnection(Protocol):
    def send(self, message: str | bytes) -> None: ...

    def recv(self, timeout: float | None = None, *, decode: bool | None = None) -> str | bytes: ...

    def close(self) -> None: ...


_Connector: TypeAlias = Callable[..., _WebSocketConnection]


@dataclass(slots=True)
class _PendingCall:
    event: threading.Event
    response: Mapping[str, Any] | None = None
    error: BrowserError | None = None


@dataclass(slots=True)
class _OutboundMessage:
    encoded: str
    sent: threading.Event
    error: BrowserError | None = None


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    payload: Mapping[str, Any]
    size_bytes: int
    session_id: str | None


@dataclass(frozen=True, slots=True)
class _InterceptionPolicy:
    authorize_url: Callable[[str], str]
    timeout: float


def validate_cdp_websocket_url(websocket_url: str) -> str:
    """Accept only an uncredentialed ``ws://`` URL on a literal loopback IP."""

    if not isinstance(websocket_url, str) or not websocket_url:
        raise BrowserValidationError("Browser DevTools WebSocket URL is invalid.")
    if len(websocket_url) > 512 or any(char.isspace() for char in websocket_url):
        raise BrowserValidationError("Browser DevTools WebSocket URL is invalid.")
    try:
        parsed = urlsplit(websocket_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise BrowserValidationError("Browser DevTools WebSocket URL is invalid.") from exc
    if (
        parsed.scheme != "ws"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or host is None
        or port is None
        or not 1 <= port <= 65_535
        or not _DEVTOOLS_PATH_RE.fullmatch(parsed.path)
    ):
        raise BrowserSecurityError("Browser DevTools WebSocket endpoint is not trusted.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise BrowserSecurityError(
            "Browser DevTools WebSocket endpoint must use a literal loopback address."
        ) from exc
    if not address.is_loopback:
        raise BrowserSecurityError("Browser DevTools WebSocket endpoint must be loopback-only.")
    return websocket_url


class WebSocketCdpTransportFactory(CdpTransportFactory):
    """Create :class:`WebSocketCdpTransport` instances with ``websockets``.

    ``connector`` is injectable for deterministic tests.  The default is the
    maintained synchronous client from the ``websockets`` package.  Proxying and
    compression are disabled for the private loopback connection.
    """

    def __init__(
        self,
        *,
        connector: _Connector | None = None,
        max_event_count: int = _MAX_EVENT_COUNT,
        max_event_bytes: int = _MAX_EVENT_BYTES,
    ) -> None:
        if not 1 <= max_event_count <= _MAX_EVENT_COUNT:
            raise BrowserValidationError("Browser CDP event count limit is invalid.")
        if not _MAX_MESSAGE_BYTES <= max_event_bytes <= _MAX_EVENT_BYTES:
            raise BrowserValidationError("Browser CDP event byte limit is invalid.")
        self._connector = connector
        self._max_event_count = max_event_count
        self._max_event_bytes = max_event_bytes

    def connect(
        self,
        websocket_url: str,
        *,
        timeout: float,
        cancel: CancelCheck = None,
    ) -> CdpTransport:
        endpoint = validate_cdp_websocket_url(websocket_url)
        call_timeout = _validated_timeout(timeout)
        _check_cancel(cancel)
        connector = self._connector or _default_connector()
        finished = threading.Event()
        abandoned = threading.Event()
        outcome_lock = threading.Lock()
        outcome: dict[str, Any] = {}

        def open_connection() -> None:
            connection: _WebSocketConnection | None = None
            try:
                connection = connector(
                    endpoint,
                    open_timeout=call_timeout,
                    close_timeout=min(call_timeout, 2.0),
                    ping_interval=20.0,
                    ping_timeout=min(call_timeout, 10.0),
                    max_size=_MAX_MESSAGE_BYTES,
                    max_queue=(32, 8),
                    compression=None,
                    proxy=None,
                )
                with outcome_lock:
                    if abandoned.is_set():
                        _close_connection_async(connection)
                    else:
                        outcome["connection"] = connection
            except BaseException as exc:  # noqa: BLE001 - normalized below
                with outcome_lock:
                    outcome["error"] = exc
            finally:
                finished.set()

        worker = threading.Thread(
            target=open_connection,
            name="alysis-cdp-connect",
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + call_timeout
        while not finished.wait(
            timeout=min(_WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
        ):
            if cancel is not None and cancel():
                with outcome_lock:
                    abandoned.set()
                    late_connection = outcome.pop("connection", None)
                if late_connection is not None:
                    _close_connection_async(late_connection)
                raise BrowserCancelledError("Browser WebSocket connection was cancelled.")
            if time.monotonic() >= deadline:
                with outcome_lock:
                    abandoned.set()
                    late_connection = outcome.pop("connection", None)
                if late_connection is not None:
                    _close_connection_async(late_connection)
                raise BrowserTimeoutError("Browser WebSocket connection timed out.")
        with outcome_lock:
            connection = outcome.get("connection")
        if cancel is not None and cancel():
            with outcome_lock:
                abandoned.set()
            if connection is not None:
                _close_connection_async(connection)
            raise BrowserCancelledError("Browser WebSocket connection was cancelled.")
        if connection is None:
            raise BrowserLaunchError(
                "Browser DevTools WebSocket connection failed."
            ) from outcome.get("error")
        return WebSocketCdpTransport(
            connection,
            max_event_count=self._max_event_count,
            max_event_bytes=self._max_event_bytes,
        )


class WebSocketCdpTransport(CdpTransport):
    """Thread-safe, bounded CDP request/reply and event multiplexer."""

    def __init__(
        self,
        connection: _WebSocketConnection,
        *,
        max_event_count: int = _MAX_EVENT_COUNT,
        max_event_bytes: int = _MAX_EVENT_BYTES,
    ) -> None:
        if not 1 <= max_event_count <= _MAX_EVENT_COUNT:
            raise BrowserValidationError("Browser CDP event count limit is invalid.")
        if not _MAX_MESSAGE_BYTES <= max_event_bytes <= _MAX_EVENT_BYTES:
            raise BrowserValidationError("Browser CDP event byte limit is invalid.")
        self._connection = connection
        self._max_event_count = max_event_count
        self._max_event_bytes = max_event_bytes
        self._lock = threading.RLock()
        self._event_changed = threading.Condition(self._lock)
        self._navigation_lock = threading.Lock()
        self._pending: dict[int, _PendingCall] = {}
        self._outbound: queue.Queue[_OutboundMessage] = queue.Queue(maxsize=_MAX_PENDING_CALLS)
        self._interception_events: queue.Queue[_QueuedEvent] = queue.Queue(maxsize=max_event_count)
        self._interception_event_bytes = 0
        self._interception_policies: dict[str, _InterceptionPolicy] = {}
        self._events: deque[_QueuedEvent] = deque()
        self._event_bytes = 0
        self._dropped_events = 0
        self._drop_generation = 0
        self._next_id = 1
        self._terminal_error: BrowserError | None = None
        self._closed = False
        self._reader = threading.Thread(
            target=self._reader_loop,
            name="alysis-cdp-reader",
            daemon=True,
        )
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="alysis-cdp-writer",
            daemon=True,
        )
        self._interceptor = threading.Thread(
            target=self._interceptor_loop,
            name="alysis-cdp-interceptor",
            daemon=True,
        )
        self._reader.start()
        self._writer.start()
        self._interceptor.start()

    @property
    def dropped_event_count(self) -> int:
        with self._lock:
            return self._dropped_events

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float,
        cancel: CancelCheck = None,
    ) -> Mapping[str, Any]:
        normalized_method = _validated_method(method)
        normalized_session = _validated_session_id(session_id)
        call_timeout = _validated_timeout(timeout)
        _check_cancel(cancel)
        deadline = time.monotonic() + call_timeout
        with self._lock:
            self._raise_if_unavailable_locked()
            if len(self._pending) >= _MAX_PENDING_CALLS:
                raise BrowserLimitError("Browser CDP pending command capacity was exhausted.")
            request_id = self._next_request_id_locked()
            pending = _PendingCall(event=threading.Event())
            self._pending[request_id] = pending
        message: dict[str, Any] = {
            "id": request_id,
            "method": normalized_method,
            "params": dict(params or {}),
        }
        if normalized_session is not None:
            message["sessionId"] = normalized_session
        try:
            encoded = _encode_message(message)
        except BaseException:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        outbound = _OutboundMessage(encoded=encoded, sent=threading.Event())
        try:
            self._outbound.put_nowait(outbound)
        except queue.Full as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise BrowserLimitError("Browser CDP outbound command capacity was exhausted.") from exc

        while not outbound.sent.wait(
            timeout=min(_WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
        ):
            if pending.event.is_set():
                break
            if cancel is not None and cancel():
                error = BrowserCancelledError("Browser CDP command was cancelled.")
                self._fail_transport(error)
                raise error
            if time.monotonic() >= deadline:
                error = BrowserTimeoutError("Browser CDP command timed out.")
                self._fail_transport(error)
                raise error
        if outbound.error is not None:
            raise outbound.error
        while not pending.event.wait(
            timeout=min(_WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
        ):
            if cancel is not None and cancel():
                error = BrowserCancelledError("Browser CDP command was cancelled.")
                self._fail_transport(error)
                raise error
            if time.monotonic() >= deadline:
                error = BrowserTimeoutError("Browser CDP command timed out.")
                self._fail_transport(error)
                raise error
        if pending.error is not None:
            raise pending.error
        response = pending.response
        if response is None:
            error = BrowserSecurityError("Browser CDP response was missing.")
            self._fail_transport(error)
            raise error
        error_payload = response.get("error")
        if error_payload is not None:
            code: int | None = None
            if isinstance(error_payload, Mapping):
                candidate = error_payload.get("code")
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and abs(candidate) <= 1_000_000
                ):
                    code = candidate
            raise CdpCommandError(normalized_method, code)
        result = response.get("result", {})
        if not isinstance(result, Mapping):
            error = BrowserSecurityError("Browser CDP result was invalid.")
            self._fail_transport(error)
            raise error
        return dict(result)

    def guarded_navigate(
        self,
        url: str,
        *,
        session_id: str,
        authorize_url: Callable[[str], str],
        timeout: float,
        cancel: CancelCheck = None,
    ) -> Mapping[str, Any]:
        normalized_session = _validated_session_id(session_id)
        if normalized_session is None:
            raise BrowserValidationError("Browser CDP session id is required for navigation.")
        if not isinstance(url, str) or not url:
            raise BrowserValidationError("Browser navigation URL is invalid.")
        if not callable(authorize_url):
            raise BrowserValidationError("Browser navigation URL policy is invalid.")
        call_timeout = _validated_timeout(timeout)
        _check_cancel(cancel)
        if not self._navigation_lock.acquire(blocking=False):
            raise BrowserSecurityError("Another guarded browser navigation is already running.")
        deadline = time.monotonic() + call_timeout
        navigation_done = threading.Event()
        navigation_outcome: dict[str, Any] = {}
        navigation_started = False
        try:
            initial_url = _authorize(authorize_url, url)
            policy = _InterceptionPolicy(
                authorize_url=authorize_url,
                timeout=min(call_timeout, 30.0),
            )
            with self._lock:
                self._raise_if_unavailable_locked()
                self._interception_policies[normalized_session] = policy
            self.call(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                },
                session_id=normalized_session,
                timeout=_remaining(deadline),
                cancel=cancel,
            )
            self.call(
                "Page.setLifecycleEventsEnabled",
                {"enabled": True},
                session_id=normalized_session,
                timeout=_remaining(deadline),
                cancel=cancel,
            )
            self.call(
                "Fetch.enable",
                {
                    "patterns": [
                        {"urlPattern": "*", "requestStage": "Request"},
                        {"urlPattern": "*", "requestStage": "Response"},
                    ],
                    "handleAuthRequests": False,
                },
                session_id=normalized_session,
                timeout=_remaining(deadline),
                cancel=cancel,
            )
            with self._lock:
                drop_generation = self._drop_generation

            def navigate() -> None:
                try:
                    navigation_outcome["result"] = self.call(
                        "Page.navigate",
                        {"url": initial_url},
                        session_id=normalized_session,
                        timeout=_remaining(deadline),
                        cancel=cancel,
                    )
                except BaseException as exc:  # noqa: BLE001 - delivered to caller below
                    navigation_outcome["error"] = exc
                finally:
                    navigation_done.set()

            threading.Thread(
                target=navigate,
                name="alysis-cdp-navigate",
                daemon=True,
            ).start()
            navigation_started = True
            loaded_loader_ids: set[str] = set()
            while True:
                _check_cancel_and_deadline(cancel, deadline, self)
                with self._lock:
                    if self._drop_generation != drop_generation:
                        error = BrowserSecurityError(
                            "Browser CDP events overflowed during guarded navigation."
                        )
                        self._fail_transport(error)
                        raise error
                event = self._take_navigation_event(
                    normalized_session,
                    timeout=min(_WAIT_POLL_SECONDS, _remaining(deadline)),
                )
                if event is not None:
                    method = event.get("method")
                    if method == "Page.lifecycleEvent":
                        params = event.get("params")
                        if isinstance(params, Mapping) and params.get("name") == "load":
                            loader_id = params.get("loaderId")
                            if isinstance(loader_id, str) and loader_id:
                                loaded_loader_ids.add(loader_id)
                if navigation_done.is_set():
                    navigation_error = navigation_outcome.get("error")
                    if navigation_error is not None:
                        if isinstance(navigation_error, BaseException):
                            raise navigation_error
                        raise BrowserLaunchError("Browser navigation failed.")
                    result = navigation_outcome.get("result")
                    if not isinstance(result, Mapping):
                        error = BrowserSecurityError("Browser navigation result was invalid.")
                        self._fail_transport(error)
                        raise error
                    error_text = result.get("errorText")
                    if error_text:
                        raise BrowserLaunchError(
                            "Browser navigation did not complete successfully."
                        )
                    loader_id = result.get("loaderId")
                    if not isinstance(loader_id, str) or not loader_id:
                        error = BrowserSecurityError(
                            "Browser navigation did not provide a trusted document loader id."
                        )
                        self._fail_transport(error)
                        raise error
                    if loader_id in loaded_loader_ids:
                        return dict(result)
        except BrowserCancelledError as exc:
            self._fail_transport(exc)
            raise
        except BrowserTimeoutError as exc:
            self._fail_transport(exc)
            raise
        except BrowserError as exc:
            # A setup or navigation failure can leave lifecycle/interception
            # state ambiguous. Never allow the transport to continue without
            # a proven persistent guard.
            self._fail_transport(exc)
            raise
        finally:
            if navigation_started and not navigation_done.is_set():
                # Never leave a Page.navigate command running after its guarded
                # operation returned.  Its late reply would otherwise race a
                # future operation and leave interception state ambiguous.
                self._fail_transport(BrowserLaunchError("Browser navigation did not stop cleanly."))
            self._navigation_lock.release()

    def drain_events(
        self,
        *,
        session_id: str,
        max_events: int,
        timeout: float,
        cancel: CancelCheck = None,
    ) -> Sequence[Mapping[str, Any]]:
        normalized_session = _validated_session_id(session_id)
        if normalized_session is None:
            raise BrowserValidationError("Browser CDP session id is required.")
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= _MAX_EVENT_DRAIN
        ):
            raise BrowserValidationError("Browser CDP event limit is invalid.")
        call_timeout = _validated_timeout(timeout)
        deadline = time.monotonic() + call_timeout
        output: list[Mapping[str, Any]] = []
        with self._event_changed:
            while True:
                self._raise_if_unavailable_locked()
                if self._dropped_events and len(output) < max_events:
                    output.append(
                        {
                            "method": "Alysis.eventsDropped",
                            "params": {"count": self._dropped_events},
                            "sessionId": normalized_session,
                        }
                    )
                    self._dropped_events = 0
                if len(output) < max_events:
                    retained: deque[_QueuedEvent] = deque()
                    while self._events:
                        item = self._events.popleft()
                        if item.session_id == normalized_session and len(output) < max_events:
                            output.append(item.payload)
                            self._event_bytes -= item.size_bytes
                        else:
                            retained.append(item)
                    self._events = retained
                if output or time.monotonic() >= deadline:
                    return output
                if cancel is not None and cancel():
                    raise BrowserCancelledError("Browser event read was cancelled.")
                self._event_changed.wait(
                    timeout=min(_WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
                )

    def close(self, *, timeout: float) -> None:
        close_timeout = _validated_timeout(timeout)
        self._fail_transport(BrowserLaunchError("Browser DevTools WebSocket was closed."))
        _close_connection_bounded(self._connection, close_timeout)
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=close_timeout)
        if threading.current_thread() is not self._writer:
            self._writer.join(timeout=close_timeout)
        if threading.current_thread() is not self._interceptor:
            self._interceptor.join(timeout=close_timeout)

    def _writer_loop(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                outbound = self._outbound.get(timeout=_READER_POLL_SECONDS)
            except queue.Empty:
                continue
            with self._lock:
                if self._closed:
                    outbound.error = self._terminal_error or BrowserLaunchError(
                        "Browser DevTools WebSocket is closed."
                    )
                    outbound.sent.set()
                    continue
            try:
                self._connection.send(outbound.encoded)
            except BaseException:  # noqa: BLE001 - normalized below
                error = BrowserLaunchError("Browser DevTools WebSocket send failed.")
                outbound.error = error
                outbound.sent.set()
                self._fail_transport(error)
                return
            outbound.sent.set()

    def _reader_loop(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                raw = self._connection.recv(timeout=_READER_POLL_SECONDS, decode=False)
            except TimeoutError:
                continue
            except BaseException:  # noqa: BLE001 - normalized below
                with self._lock:
                    if self._closed:
                        return
                error = BrowserLaunchError("Browser DevTools WebSocket receive failed.")
                self._fail_transport(error)
                _close_connection_async(self._connection)
                return
            try:
                payload, size_bytes = _decode_message(raw)
                self._route_message(payload, size_bytes=size_bytes)
            except BrowserError as exc:
                self._fail_transport(exc)
                _close_connection_async(self._connection)
                return
            except BaseException:  # noqa: BLE001 - decoder bugs fail closed
                error = BrowserSecurityError("Browser CDP message was invalid.")
                self._fail_transport(error)
                _close_connection_async(self._connection)
                return

    def _interceptor_loop(self) -> None:
        """Continue every policy-approved request for the transport lifetime."""

        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                item = self._interception_events.get(timeout=_READER_POLL_SECONDS)
            except queue.Empty:
                continue
            with self._lock:
                self._interception_event_bytes -= item.size_bytes
                if self._interception_event_bytes < 0:
                    error = BrowserSecurityError(
                        "Browser request interception accounting failed closed."
                    )
                    self._interception_event_bytes = 0
                    self._fail_transport(error)
                    return
            session_id = item.session_id
            if session_id is None:
                error = BrowserSecurityError("Browser intercepted request had no session id.")
                self._fail_transport(error)
                return
            with self._lock:
                policy = self._interception_policies.get(session_id)
            if policy is None:
                error = BrowserSecurityError("Browser request interception policy was unavailable.")
                self._fail_transport(error)
                return
            try:
                method = item.payload.get("method")
                if method == "Target.attachedToTarget":
                    self._guard_attached_target(
                        item.payload,
                        parent_session_id=session_id,
                        policy=policy,
                    )
                    continue
                if method == "Target.detachedFromTarget":
                    self._forget_detached_target(item.payload)
                    continue
                if method == "Fetch.authRequired":
                    raise BrowserSecurityError(
                        "Browser authentication challenge was blocked during navigation."
                    )
                if method != "Fetch.requestPaused":
                    raise BrowserSecurityError("Browser interception event was invalid.")
                _continue_paused_request(
                    self,
                    item.payload,
                    session_id=session_id,
                    authorize_url=policy.authorize_url,
                    deadline=time.monotonic() + policy.timeout,
                    cancel=None,
                )
            except BrowserError as exc:
                self._fail_transport(exc)
                return
            except BaseException as exc:  # noqa: BLE001 - policy broker fails closed
                error = BrowserSecurityError("Browser request interception failed closed.")
                self._fail_transport(error)
                _close_connection_async(self._connection)
                _ = exc
                return

    def _guard_attached_target(
        self,
        event: Mapping[str, Any],
        *,
        parent_session_id: str,
        policy: _InterceptionPolicy,
    ) -> None:
        """Install the same guard before a popup/worker child is resumed."""

        params = event.get("params")
        if not isinstance(params, Mapping):
            raise BrowserSecurityError("Browser attached-target event was invalid.")
        child_session_id = params.get("sessionId")
        waiting = params.get("waitingForDebugger")
        if (
            not isinstance(child_session_id, str)
            or not child_session_id
            or len(child_session_id) > _MAX_SESSION_ID_CHARS
            or waiting is not True
            or child_session_id == parent_session_id
        ):
            raise BrowserSecurityError("Browser attached target was not safely paused.")
        with self._lock:
            self._raise_if_unavailable_locked()
            self._interception_policies[child_session_id] = policy
        deadline = time.monotonic() + policy.timeout
        self.call(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
            },
            session_id=child_session_id,
            timeout=_remaining(deadline),
        )
        self.call(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "*", "requestStage": "Request"},
                    {"urlPattern": "*", "requestStage": "Response"},
                ],
                "handleAuthRequests": False,
            },
            session_id=child_session_id,
            timeout=_remaining(deadline),
        )
        self.call(
            "Runtime.runIfWaitingForDebugger",
            {},
            session_id=child_session_id,
            timeout=_remaining(deadline),
        )

    def _forget_detached_target(self, event: Mapping[str, Any]) -> None:
        params = event.get("params")
        if not isinstance(params, Mapping):
            raise BrowserSecurityError("Browser detached-target event was invalid.")
        child_session_id = params.get("sessionId")
        if not isinstance(child_session_id, str) or not child_session_id:
            raise BrowserSecurityError("Browser detached-target event was invalid.")
        with self._lock:
            self._interception_policies.pop(child_session_id, None)

    def _route_message(self, payload: Mapping[str, Any], *, size_bytes: int) -> None:
        if "id" in payload:
            if "method" in payload:
                raise BrowserSecurityError("Browser CDP response shape was invalid.")
            request_id = payload.get("id")
            if (
                isinstance(request_id, bool)
                or not isinstance(request_id, int)
                or not 1 <= request_id <= _MAX_RESPONSE_ID
            ):
                raise BrowserSecurityError("Browser CDP response id was invalid.")
            with self._lock:
                pending = self._pending.pop(request_id, None)
                if pending is None:
                    raise BrowserSecurityError("Browser CDP response id was unknown or duplicated.")
                pending.response = payload
                pending.event.set()
            return

        method = payload.get("method")
        params = payload.get("params", {})
        session_id = payload.get("sessionId")
        if not isinstance(method, str) or not _METHOD_RE.fullmatch(method):
            raise BrowserSecurityError("Browser CDP event method was invalid.")
        if not isinstance(params, Mapping):
            raise BrowserSecurityError("Browser CDP event params were invalid.")
        if session_id is not None and (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > _MAX_SESSION_ID_CHARS
            or any(ord(char) < 0x20 for char in session_id)
        ):
            raise BrowserSecurityError("Browser CDP event session id was invalid.")
        queued = _QueuedEvent(payload=payload, size_bytes=size_bytes, session_id=session_id)
        if method in {
            "Fetch.authRequired",
            "Fetch.requestPaused",
            "Target.attachedToTarget",
            "Target.detachedFromTarget",
        }:
            with self._lock:
                guarded = session_id is not None and session_id in self._interception_policies
                if method == "Target.detachedFromTarget" and isinstance(params, Mapping):
                    detached = params.get("sessionId")
                    guarded = guarded or (
                        isinstance(detached, str) and detached in self._interception_policies
                    )
            if guarded:
                with self._lock:
                    if (
                        size_bytes > self._max_event_bytes
                        or self._interception_event_bytes + size_bytes > self._max_event_bytes
                    ):
                        raise BrowserSecurityError(
                            "Browser request interception byte budget overflowed."
                        )
                    try:
                        self._interception_events.put_nowait(queued)
                    except queue.Full as exc:
                        raise BrowserSecurityError(
                            "Browser request interception queue overflowed."
                        ) from exc
                    self._interception_event_bytes += size_bytes
                return
        with self._event_changed:
            while self._events and (
                len(self._events) >= self._max_event_count
                or self._event_bytes + size_bytes > self._max_event_bytes
            ):
                removed = self._events.popleft()
                self._event_bytes -= removed.size_bytes
                self._dropped_events += 1
                self._drop_generation += 1
            if size_bytes > self._max_event_bytes:
                self._dropped_events += 1
                self._drop_generation += 1
            else:
                self._events.append(queued)
                self._event_bytes += size_bytes
            self._event_changed.notify_all()

    def _take_navigation_event(
        self,
        session_id: str,
        *,
        timeout: float,
    ) -> Mapping[str, Any] | None:
        wanted = {"Page.lifecycleEvent"}
        deadline = time.monotonic() + max(0.0, timeout)
        with self._event_changed:
            while True:
                self._raise_if_unavailable_locked()
                for index, item in enumerate(self._events):
                    if item.session_id == session_id and item.payload.get("method") in wanted:
                        del self._events[index]
                        self._event_bytes -= item.size_bytes
                        return item.payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._event_changed.wait(timeout=min(_WAIT_POLL_SECONDS, remaining))

    def _next_request_id_locked(self) -> int:
        if self._next_id > _MAX_RESPONSE_ID:
            raise BrowserSecurityError("Browser CDP request id space was exhausted.")
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _raise_if_unavailable_locked(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._closed:
            raise BrowserLaunchError("Browser DevTools WebSocket is closed.")

    def _fail_transport(self, error: BrowserError) -> None:
        should_close = False
        with self._event_changed:
            if self._terminal_error is None:
                self._terminal_error = error
                should_close = True
            self._closed = True
            pending = tuple(self._pending.values())
            self._pending.clear()
            for item in pending:
                item.error = self._terminal_error
                item.event.set()
            while True:
                try:
                    outbound = self._outbound.get_nowait()
                except queue.Empty:
                    break
                outbound.error = self._terminal_error
                outbound.sent.set()
            self._event_changed.notify_all()
        if should_close:
            _close_connection_async(self._connection)


def _continue_paused_request(
    transport: WebSocketCdpTransport,
    event: Mapping[str, Any],
    *,
    session_id: str,
    authorize_url: Callable[[str], str],
    deadline: float,
    cancel: CancelCheck,
) -> str:
    params = event.get("params")
    if not isinstance(params, Mapping):
        error = BrowserSecurityError("Browser paused request was invalid.")
        transport._fail_transport(error)
        raise error
    request_id = params.get("requestId")
    request = params.get("request")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 256
        or not isinstance(request, Mapping)
    ):
        error = BrowserSecurityError("Browser paused request was invalid.")
        transport._fail_transport(error)
        raise error
    candidate = request.get("url")
    if not isinstance(candidate, str) or not candidate:
        error = BrowserSecurityError("Browser paused request URL was invalid.")
        transport._fail_transport(error)
        raise error
    response_stage = "responseStatusCode" in params or "responseErrorReason" in params
    try:
        authorized = _authorize(authorize_url, candidate)
    except BaseException as exc:
        try:
            transport.call(
                "Fetch.failRequest",
                {"requestId": request_id, "errorReason": "BlockedByClient"},
                session_id=session_id,
                timeout=_remaining(deadline),
                cancel=cancel,
            )
        except BrowserError:
            transport._fail_transport(
                BrowserSecurityError("Browser rejected request could not be failed safely.")
            )
        if isinstance(exc, BrowserError):
            raise exc
        raise BrowserSecurityError("Browser request URL policy failed closed.") from exc
    if response_stage:
        transport.call(
            "Fetch.continueResponse",
            {"requestId": request_id},
            session_id=session_id,
            timeout=_remaining(deadline),
            cancel=cancel,
        )
        return "Response"
    continue_params: dict[str, Any] = {"requestId": request_id}
    if authorized != candidate:
        continue_params["url"] = authorized
    transport.call(
        "Fetch.continueRequest",
        continue_params,
        session_id=session_id,
        timeout=_remaining(deadline),
        cancel=cancel,
    )
    return "Request"


def _default_connector() -> _Connector:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:  # pragma: no cover - installation contract
        raise BrowserDependencyError(
            "Managed browser automation requires the websockets package."
        ) from exc
    return connect


def _encode_message(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BrowserValidationError("Browser CDP command was not valid bounded JSON.") from exc
    if len(encoded) > _MAX_MESSAGE_BYTES:
        raise BrowserValidationError("Browser CDP command exceeded the 8 MiB limit.")
    return encoded.decode("utf-8")


def _decode_message(raw: str | bytes) -> tuple[Mapping[str, Any], int]:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
        except UnicodeError as exc:
            raise BrowserSecurityError("Browser CDP frame was not valid UTF-8.") from exc
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise BrowserSecurityError("Browser CDP frame type was invalid.")
    if not encoded or len(encoded) > _MAX_MESSAGE_BYTES:
        raise BrowserSecurityError("Browser CDP frame exceeded the 8 MiB limit.")

    def reject_constant(value: str) -> None:
        raise ValueError(f"unsupported JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            encoded,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BrowserSecurityError("Browser CDP frame was not valid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise BrowserSecurityError("Browser CDP message was not an object.")
    try:
        decoded_size = len(
            json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise BrowserSecurityError("Browser CDP decoded message was invalid.") from exc
    if decoded_size > _MAX_MESSAGE_BYTES:
        raise BrowserSecurityError("Browser CDP decoded message exceeded the 8 MiB limit.")
    return payload, decoded_size


def _validated_method(method: str) -> str:
    if (
        not isinstance(method, str)
        or len(method) > _MAX_METHOD_CHARS
        or not _METHOD_RE.fullmatch(method)
    ):
        raise BrowserValidationError("Browser CDP method is invalid.")
    return method


def _validated_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > _MAX_SESSION_ID_CHARS
        or any(ord(char) < 0x20 for char in session_id)
    ):
        raise BrowserValidationError("Browser CDP session id is invalid.")
    return session_id


def _validated_timeout(timeout: float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(float(timeout))
        or not 0.05 <= float(timeout) <= 300.0
    ):
        raise BrowserValidationError("Browser CDP timeout is invalid.")
    return float(timeout)


def _authorize(authorize_url: Callable[[str], str], candidate: str) -> str:
    try:
        authorized = authorize_url(candidate)
    except BrowserError:
        raise
    except BaseException as exc:  # noqa: BLE001 - policy failure must fail closed
        raise BrowserSecurityError("Browser request URL policy failed closed.") from exc
    if not isinstance(authorized, str) or not authorized:
        raise BrowserSecurityError("Browser request URL policy returned an invalid URL.")
    return authorized


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining < 0.05:
        raise BrowserTimeoutError("Browser CDP operation timed out.")
    return remaining


def _check_cancel(cancel: CancelCheck) -> None:
    if cancel is not None and cancel():
        raise BrowserCancelledError("Browser operation was cancelled.")


def _check_cancel_and_deadline(
    cancel: CancelCheck,
    deadline: float,
    transport: WebSocketCdpTransport,
) -> None:
    if cancel is not None and cancel():
        error = BrowserCancelledError("Browser navigation was cancelled.")
        transport._fail_transport(error)
        raise error
    if time.monotonic() >= deadline:
        error = BrowserTimeoutError("Browser navigation timed out.")
        transport._fail_transport(error)
        raise error


def _close_connection_async(connection: _WebSocketConnection) -> None:
    threading.Thread(
        target=_close_connection_safely,
        args=(connection,),
        name="alysis-cdp-close",
        daemon=True,
    ).start()


def _close_connection_bounded(connection: _WebSocketConnection, timeout: float) -> None:
    worker = threading.Thread(
        target=_close_connection_safely,
        args=(connection,),
        name="alysis-cdp-close",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=timeout)


def _close_connection_safely(connection: _WebSocketConnection) -> None:
    try:
        connection.close()
    except BaseException:  # noqa: BLE001 - cleanup must not mask the original failure
        return
