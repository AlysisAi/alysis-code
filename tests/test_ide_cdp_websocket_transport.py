from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from websockets.sync.server import ServerConnection, serve

from alysis_code.ide.cdp_websocket_transport import (
    WebSocketCdpTransport,
    WebSocketCdpTransportFactory,
    validate_cdp_websocket_url,
)
from alysis_code.ide.managed_browser import (
    BrowserCancelledError,
    BrowserSecurityError,
    BrowserTimeoutError,
)

_CLOSED = object()


class FakeWebSocket:
    def __init__(self) -> None:
        self.inbound: queue.Queue[object] = queue.Queue()
        self.outbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self.closed = threading.Event()
        self.close_calls = 0

    def send(self, message: str | bytes) -> None:
        if self.closed.is_set():
            raise ConnectionError("closed")
        self.outbound.put(json.loads(message))

    def recv(self, timeout: float | None = None, *, decode: bool | None = None) -> str | bytes:
        del decode
        try:
            value = self.inbound.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if value is _CLOSED:
            raise ConnectionError("closed")
        assert isinstance(value, str | bytes)
        return value

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()
        self.inbound.put(_CLOSED)

    def receive_json(self, payload: dict[str, Any]) -> None:
        self.inbound.put(json.dumps(payload, separators=(",", ":")))

    def next_command(self, method: str, timeout: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        unmatched: list[dict[str, Any]] = []
        try:
            while time.monotonic() < deadline:
                try:
                    command = self.outbound.get(timeout=min(0.05, deadline - time.monotonic()))
                except queue.Empty:
                    continue
                if command.get("method") == method:
                    return command
                unmatched.append(command)
        finally:
            for command in unmatched:
                self.outbound.put(command)
        raise AssertionError(f"CDP command {method} was not sent")

    def respond(self, command: dict[str, Any], result: dict[str, Any] | None = None) -> None:
        self.receive_json({"id": command["id"], "result": result or {}})


@pytest.mark.parametrize(
    "url",
    (
        "wss://127.0.0.1:9222/devtools/browser/id",
        "ws://localhost:9222/devtools/browser/id",
        "ws://192.168.1.4:9222/devtools/browser/id",
        "ws://127.0.0.1:9222/json/version",
        "ws://user:secret@127.0.0.1:9222/devtools/browser/id",
        "ws://127.0.0.1:9222/devtools/browser/id?token=value",
    ),
)
def test_endpoint_validation_rejects_nonliteral_or_untrusted_urls(url: str) -> None:
    with pytest.raises(BrowserSecurityError):
        validate_cdp_websocket_url(url)


@pytest.mark.parametrize(
    "url",
    (
        "ws://127.0.0.1:9222/devtools/browser/id-1",
        "ws://127.12.34.56:65535/devtools/browser/browser.1",
        "ws://[::1]:9222/devtools/browser/id_1",
    ),
)
def test_endpoint_validation_accepts_literal_loopback_urls(url: str) -> None:
    assert validate_cdp_websocket_url(url) == url


def test_factory_disables_proxy_compression_and_caps_messages() -> None:
    connection = FakeWebSocket()
    captured: dict[str, Any] = {}

    def connector(url: str, **kwargs: Any) -> FakeWebSocket:
        captured["url"] = url
        captured.update(kwargs)
        return connection

    transport = WebSocketCdpTransportFactory(connector=connector).connect(
        "ws://127.0.0.1:9222/devtools/browser/id",
        timeout=0.5,
    )
    try:
        assert captured["proxy"] is None
        assert captured["compression"] is None
        assert captured["max_size"] == 8 * 1024 * 1024
        assert captured["url"].startswith("ws://127.0.0.1:")
    finally:
        transport.close(timeout=0.2)


def test_default_websockets_adapter_round_trips_against_loopback_server() -> None:
    def handler(websocket: ServerConnection) -> None:
        command = json.loads(websocket.recv())
        websocket.send(json.dumps({"id": command["id"], "result": {"ready": True}}))

    server = serve(handler, "127.0.0.1", 0)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    port = server.socket.getsockname()[1]
    transport = WebSocketCdpTransportFactory().connect(
        f"ws://127.0.0.1:{port}/devtools/browser/integration",
        timeout=1,
    )
    try:
        assert transport.call("Browser.getVersion", timeout=1) == {"ready": True}
    finally:
        transport.close(timeout=0.2)
        server.shutdown()
        server_thread.join(timeout=1)


@pytest.mark.parametrize("cancelled", (False, True), ids=("timeout", "cancel"))
def test_factory_interrupts_slow_connect_and_closes_late_connection(cancelled: bool) -> None:
    connection = FakeWebSocket()
    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()

    def connector(url: str, **kwargs: Any) -> FakeWebSocket:
        del url, kwargs
        entered.set()
        release.wait(timeout=1)
        return connection

    if cancelled:
        threading.Thread(
            target=lambda: (entered.wait(timeout=1), cancel.set()),
            daemon=True,
        ).start()
    expected = BrowserCancelledError if cancelled else BrowserTimeoutError
    with pytest.raises(expected):
        WebSocketCdpTransportFactory(connector=connector).connect(
            "ws://127.0.0.1:9222/devtools/browser/id",
            timeout=0.05 if not cancelled else 1,
            cancel=cancel.is_set if cancelled else None,
        )
    release.set()
    assert connection.closed.wait(timeout=1)


def test_concurrent_calls_correlate_out_of_order_responses() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    results: dict[str, Any] = {}

    def invoke(name: str) -> None:
        results[name] = transport.call(name, timeout=1.0)

    first = threading.Thread(target=invoke, args=("Runtime.evaluate",))
    second = threading.Thread(target=invoke, args=("DOM.getDocument",))
    first.start()
    second.start()
    commands = [connection.outbound.get(timeout=1), connection.outbound.get(timeout=1)]
    for command in reversed(commands):
        connection.respond(command, {"method": command["method"]})
    first.join(timeout=1)
    second.join(timeout=1)
    try:
        assert results["Runtime.evaluate"] == {"method": "Runtime.evaluate"}
        assert results["DOM.getDocument"] == {"method": "DOM.getDocument"}
    finally:
        transport.close(timeout=0.2)


def test_unknown_and_duplicate_response_ids_fail_transport() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    connection.receive_json({"id": 999, "result": {}})
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="unknown or duplicated"):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_duplicate_response_id_after_completed_call_fails_transport() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    result: dict[str, Any] = {}

    def call() -> None:
        result.update(transport.call("Runtime.evaluate", timeout=1))

    thread = threading.Thread(target=call)
    thread.start()
    command = connection.outbound.get(timeout=1)
    connection.respond(command, {"value": 1})
    thread.join(timeout=1)
    assert result == {"value": 1}
    connection.respond(command, {"value": 2})
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="unknown or duplicated"):
        transport.call("DOM.getDocument", timeout=0.2)


@pytest.mark.parametrize(
    "payload",
    (
        b'{"id":1,"id":1,"result":{}}',
        b'{"method":"Runtime.event","params":{"value":NaN}}',
        b"[]",
        b"x" * (8 * 1024 * 1024 + 1),
    ),
    ids=("duplicate-key", "non-finite", "non-object", "oversized"),
)
def test_malformed_or_oversized_frames_fail_closed(payload: bytes) -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    connection.inbound.put(payload)
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_timeout_interrupts_all_pending_calls_and_closes_connection() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    started = time.monotonic()
    with pytest.raises(BrowserTimeoutError):
        transport.call("Runtime.evaluate", timeout=0.05)
    assert time.monotonic() - started < 0.5
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserTimeoutError):
        transport.call("DOM.getDocument", timeout=0.2)


def test_timeout_interrupts_a_blocked_socket_write() -> None:
    class BlockingSendWebSocket(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.send_entered = threading.Event()
            self.send_release = threading.Event()

        def send(self, message: str | bytes) -> None:
            self.send_entered.set()
            self.send_release.wait(timeout=1)
            if not self.closed.is_set():
                super().send(message)

        def close(self) -> None:
            self.send_release.set()
            super().close()

    connection = BlockingSendWebSocket()
    transport = WebSocketCdpTransport(connection)
    started = time.monotonic()
    with pytest.raises(BrowserTimeoutError):
        transport.call("Runtime.evaluate", timeout=0.05)
    assert connection.send_entered.is_set()
    assert time.monotonic() - started < 0.5
    assert connection.closed.wait(timeout=1)


def test_cancellation_interrupts_pending_call() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    cancelled = threading.Event()

    def cancel() -> bool:
        return cancelled.is_set()

    def trigger() -> None:
        connection.outbound.get(timeout=1)
        cancelled.set()

    threading.Thread(target=trigger, daemon=True).start()
    with pytest.raises(BrowserCancelledError):
        transport.call("Runtime.evaluate", timeout=1, cancel=cancel)
    assert connection.closed.wait(timeout=1)


def test_event_queue_is_bounded_and_reports_drops() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection, max_event_count=2)
    for index in range(3):
        connection.receive_json(
            {
                "method": "Runtime.consoleAPICalled",
                "params": {"index": index},
                "sessionId": "session-1",
            }
        )
    deadline = time.monotonic() + 1
    while transport.dropped_event_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    events = transport.drain_events(session_id="session-1", max_events=10, timeout=0.2)
    try:
        assert events[0] == {
            "method": "Alysis.eventsDropped",
            "params": {"count": 1},
            "sessionId": "session-1",
        }
        assert [event["params"]["index"] for event in events[1:]] == [1, 2]
    finally:
        transport.close(timeout=0.2)


def _run_guarded_navigation(
    transport: WebSocketCdpTransport,
    authorize: Callable[[str], str],
    outcome: dict[str, Any],
) -> threading.Thread:
    def run() -> None:
        try:
            outcome["result"] = transport.guarded_navigate(
                "https://example.com/",
                session_id="session-1",
                authorize_url=authorize,
                timeout=2,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted by tests
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _pause(
    connection: FakeWebSocket,
    request_id: str,
    url: str,
    *,
    response: bool = False,
    session_id: str = "session-1",
    padding: str | None = None,
) -> None:
    params: dict[str, Any] = {
        "requestId": request_id,
        "request": {"url": url},
    }
    if response:
        params["responseStatusCode"] = 200
    if padding is not None:
        params["padding"] = padding
    connection.receive_json(
        {
            "method": "Fetch.requestPaused",
            "params": params,
            "sessionId": session_id,
        }
    )


def _enable_persistent_guard(connection: FakeWebSocket) -> dict[str, Any]:
    auto_attach = connection.next_command("Target.setAutoAttach")
    assert auto_attach["params"] == {
        "autoAttach": True,
        "waitForDebuggerOnStart": True,
        "flatten": True,
    }
    connection.respond(auto_attach)
    lifecycle = connection.next_command("Page.setLifecycleEventsEnabled")
    assert lifecycle["params"] == {"enabled": True}
    connection.respond(lifecycle)
    enable = connection.next_command("Fetch.enable")
    connection.respond(enable)
    return enable


def _emit_document_load(connection: FakeWebSocket, loader_id: str) -> None:
    connection.receive_json(
        {
            "method": "Page.lifecycleEvent",
            "params": {
                "frameId": "frame-1",
                "loaderId": loader_id,
                "name": "load",
                "timestamp": 1.0,
            },
            "sessionId": "session-1",
        }
    )


def _complete_public_navigation(
    transport: WebSocketCdpTransport,
    connection: FakeWebSocket,
    authorize: Callable[[str], str],
) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, authorize, outcome)
    _enable_persistent_guard(connection)
    navigate = connection.next_command("Page.navigate")
    _pause(connection, "initial-request", "https://example.com/")
    connection.respond(connection.next_command("Fetch.continueRequest"))
    _pause(connection, "initial-response", "https://example.com/", response=True)
    connection.respond(connection.next_command("Fetch.continueResponse"))
    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "loader-1"})
    _emit_document_load(connection, "loader-1")
    thread.join(timeout=1)
    assert thread.is_alive() is False
    assert "error" not in outcome
    return outcome["result"]


def test_guarded_navigation_authorizes_redirects_subresources_and_both_stages() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    authorized: list[str] = []

    def authorize(url: str) -> str:
        authorized.append(url)
        return url

    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, authorize, outcome)
    enable = _enable_persistent_guard(connection)
    assert enable["params"]["patterns"] == [
        {"urlPattern": "*", "requestStage": "Request"},
        {"urlPattern": "*", "requestStage": "Response"},
    ]
    navigate = connection.next_command("Page.navigate")

    stages = (
        ("top-request", "https://example.com/", False, "Fetch.continueRequest"),
        ("top-response", "https://example.com/", True, "Fetch.continueResponse"),
        ("redirect", "https://www.example.com/redirected", False, "Fetch.continueRequest"),
        ("asset-request", "https://cdn.example.com/app.js", False, "Fetch.continueRequest"),
        ("asset-response", "https://cdn.example.com/app.js", True, "Fetch.continueResponse"),
    )
    for request_id, url, response, expected_method in stages:
        _pause(connection, request_id, url, response=response)
        command = connection.next_command(expected_method)
        assert command["params"]["requestId"] == request_id
        connection.respond(command)

    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "loader-1"})
    _emit_document_load(connection, "loader-1")
    thread.join(timeout=1)
    try:
        assert outcome == {"result": {"frameId": "frame-1", "loaderId": "loader-1"}}
        assert authorized == [
            "https://example.com/",
            "https://example.com/",
            "https://example.com/",
            "https://www.example.com/redirected",
            "https://cdn.example.com/app.js",
            "https://cdn.example.com/app.js",
        ]
    finally:
        transport.close(timeout=0.2)


def test_persistent_guard_authorizes_post_load_xhr() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    authorized: list[str] = []

    def authorize(url: str) -> str:
        authorized.append(url)
        return url

    try:
        _complete_public_navigation(transport, connection, authorize)
        _pause(connection, "post-load-xhr", "https://api.example.com/data")
        continued = connection.next_command("Fetch.continueRequest")
        assert continued["params"]["requestId"] == "post-load-xhr"
        connection.respond(continued)
        deadline = time.monotonic() + 1
        while "https://api.example.com/data" not in authorized and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "https://api.example.com/data" in authorized
        assert connection.closed.is_set() is False
    finally:
        transport.close(timeout=0.2)


@pytest.mark.parametrize(
    ("method", "params"),
    (
        ("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 1, "y": 1}),
        ("Input.insertText", {"text": "submit"}),
    ),
    ids=("click-triggered-navigation", "type-triggered-submission"),
)
def test_persistent_guard_blocks_private_request_during_input_action(
    method: str,
    params: dict[str, Any],
) -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "127.0.0.1" in url:
            raise BrowserSecurityError("private destination")
        return url

    _complete_public_navigation(transport, connection, authorize)
    outcome: dict[str, BaseException] = {}

    def input_action() -> None:
        try:
            transport.call(method, params, session_id="session-1", timeout=1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            outcome["error"] = exc

    action = threading.Thread(target=input_action)
    action.start()
    connection.next_command(method)
    _pause(connection, "private-after-input", "http://127.0.0.1/admin")
    failed = connection.next_command("Fetch.failRequest")
    assert failed["params"] == {
        "requestId": "private-after-input",
        "errorReason": "BlockedByClient",
    }
    connection.respond(failed)
    assert connection.closed.wait(timeout=1)
    action.join(timeout=1)
    assert isinstance(outcome.get("error"), BrowserSecurityError)


def test_persistent_guard_pauses_and_guards_popup_before_resuming_it() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "127.0.0.1" in url:
            raise BrowserSecurityError("private destination")
        return url

    _complete_public_navigation(transport, connection, authorize)
    connection.receive_json(
        {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": "popup-session",
                "waitingForDebugger": True,
                "targetInfo": {
                    "targetId": "popup-target",
                    "type": "page",
                    "url": "",
                },
            },
            "sessionId": "session-1",
        }
    )
    child_auto_attach = connection.next_command("Target.setAutoAttach")
    assert child_auto_attach["sessionId"] == "popup-session"
    assert child_auto_attach["params"]["waitForDebuggerOnStart"] is True
    connection.respond(child_auto_attach)
    child_fetch = connection.next_command("Fetch.enable")
    assert child_fetch["sessionId"] == "popup-session"
    assert child_fetch["params"]["patterns"] == [
        {"urlPattern": "*", "requestStage": "Request"},
        {"urlPattern": "*", "requestStage": "Response"},
    ]
    connection.respond(child_fetch)
    resume = connection.next_command("Runtime.runIfWaitingForDebugger")
    assert resume["sessionId"] == "popup-session"
    connection.respond(resume)

    _pause(
        connection,
        "private-popup-request",
        "http://127.0.0.1/private",
        session_id="popup-session",
    )
    failed = connection.next_command("Fetch.failRequest")
    assert failed["sessionId"] == "popup-session"
    assert failed["params"] == {
        "requestId": "private-popup-request",
        "errorReason": "BlockedByClient",
    }
    connection.respond(failed)
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="private destination"):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_persistent_guard_fails_closed_for_unpaused_child_target() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    _complete_public_navigation(transport, connection, lambda url: url)

    connection.receive_json(
        {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": "unsafe-child",
                "waitingForDebugger": False,
                "targetInfo": {
                    "targetId": "unsafe-target",
                    "type": "worker",
                    "url": "https://example.com/worker.js",
                },
            },
            "sessionId": "session-1",
        }
    )

    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="not safely paused"):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_persistent_guard_interception_queue_has_hard_byte_budget() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(
        connection,
        max_event_bytes=8 * 1024 * 1024,
    )
    _complete_public_navigation(transport, connection, lambda url: url)

    # Keep the broker occupied on one request while two individually valid
    # frames attempt to consume more than the aggregate interception budget.
    padding = "x" * (4 * 1024 * 1024 + 128 * 1024)
    _pause(connection, "broker-blocker", "https://example.com/one", padding=padding)
    connection.next_command("Fetch.continueRequest")
    _pause(connection, "queued-two", "https://example.com/two", padding=padding)
    _pause(connection, "queued-three", "https://example.com/three", padding=padding)

    assert connection.closed.wait(timeout=2)
    with pytest.raises(BrowserSecurityError, match="byte budget overflowed"):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_guarded_navigation_fails_rejected_redirect_without_continuing_it() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "127.0.0.1" in url:
            raise BrowserSecurityError("private destination")
        return url

    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, authorize, outcome)
    _enable_persistent_guard(connection)
    connection.next_command("Page.navigate")
    _pause(connection, "top", "https://example.com/")
    continued = connection.next_command("Fetch.continueRequest")
    connection.respond(continued)
    _pause(connection, "blocked", "http://127.0.0.1/secrets")
    failed = connection.next_command("Fetch.failRequest")
    assert failed["params"] == {"requestId": "blocked", "errorReason": "BlockedByClient"}
    connection.respond(failed)
    assert connection.closed.wait(timeout=1)
    thread.join(timeout=1)
    try:
        assert isinstance(outcome.get("error"), BrowserSecurityError)
        remaining = list(connection.outbound.queue)
        assert not any(
            command.get("method") == "Fetch.continueRequest"
            and command.get("params", {}).get("requestId") == "blocked"
            for command in remaining
        )
    finally:
        transport.close(timeout=0.2)


def test_stale_load_events_cannot_complete_a_new_navigation() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    # Both legacy load events and lifecycle events from an older loader may be
    # queued before the new Page.navigate command is acknowledged.
    connection.receive_json(
        {
            "method": "Page.loadEventFired",
            "params": {"timestamp": 0.5},
            "sessionId": "session-1",
        }
    )
    _emit_document_load(connection, "old-loader")
    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, lambda url: url, outcome)
    _enable_persistent_guard(connection)
    navigate = connection.next_command("Page.navigate")
    _pause(connection, "top", "https://example.com/")
    continued = connection.next_command("Fetch.continueRequest")
    connection.respond(continued)
    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "new-loader"})
    time.sleep(0.1)
    assert thread.is_alive(), "stale loader event ended guarded navigation"
    _emit_document_load(connection, "new-loader")
    thread.join(timeout=1)
    try:
        assert outcome == {"result": {"frameId": "frame-1", "loaderId": "new-loader"}}
    finally:
        transport.close(timeout=0.2)
