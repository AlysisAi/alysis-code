from __future__ import annotations

import html
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .oauth import (
    McpAuthorizationServerMetadata,
    McpOAuthCallbackError,
    build_authorization_url,
    build_pkce_challenge,
    exchange_authorization_code,
    generate_oauth_state,
    generate_pkce_verifier,
)
from .oauth_store import McpOAuthTokenRecord, save_oauth_token_record

__all__ = [
    "McpOAuthCallbackPayload",
    "McpOAuthLoginResult",
    "perform_authorization_code_login",
]

_CALLBACK_PATH = "/oauth/callback"
_LOCALHOST_CALLBACK_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class McpOAuthCallbackPayload:
    code: str | None
    state: str | None
    error: str | None = None
    error_description: str | None = None


@dataclass(frozen=True)
class McpOAuthLoginResult:
    server_id: str
    authorization_url: str
    redirect_uri: str
    browser_opened: bool
    token_record: McpOAuthTokenRecord


class _CallbackState:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.payload: McpOAuthCallbackPayload | None = None
        self._lock = threading.Lock()

    def set_payload(self, payload: McpOAuthCallbackPayload) -> None:
        with self._lock:
            if self.payload is None:
                self.payload = payload
                self.event.set()


class _CallbackListenerHttpServer(ThreadingHTTPServer):
    daemon_threads = True


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "AlysisOAuthCallback/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    @property
    def callback_state(self) -> _CallbackState:
        return self.server.callback_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path != _CALLBACK_PATH:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(parsed.query)
        payload = McpOAuthCallbackPayload(
            code=(query.get("code") or [None])[0],
            state=(query.get("state") or [None])[0],
            error=(query.get("error") or [None])[0],
            error_description=(query.get("error_description") or [None])[0],
        )
        self.callback_state.set_payload(payload)
        message = (
            "OAuth login failed. Return to the terminal for details."
            if payload.error
            else "OAuth login complete. You can close this window and return to the terminal."
        )
        self._send_html(HTTPStatus.OK, message)

    def _send_html(self, status: int, message: str) -> None:
        safe_message = html.escape(message)
        body = (
            '<!doctype html><html><head><meta charset="utf-8"></head>'
            f"<body><p>{safe_message}</p></body></html>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _LocalhostCallbackListener:
    def __init__(
        self,
        *,
        server_id: str,
        authorization_server_url: str,
        host: str,
        port: int,
    ) -> None:
        self.server_id = server_id
        self.authorization_server_url = authorization_server_url
        self.callback_state = _CallbackState()
        self._server = _CallbackListenerHttpServer((host, port), _CallbackHandler)
        self._server.callback_state = self.callback_state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._closed = False

    @property
    def redirect_uri(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}{_CALLBACK_PATH}"

    def wait_for_callback(
        self,
        *,
        timeout_s: float,
        interrupt_check: Callable[[], None] | None = None,
    ) -> McpOAuthCallbackPayload:
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpOAuthCallbackError(
                        f"timed out waiting {int(timeout_s)}s for the localhost OAuth callback.",
                        server_id=self.server_id,
                        authorization_server_url=self.authorization_server_url,
                    )
                if interrupt_check is not None:
                    interrupt_check()
                if self.callback_state.event.wait(timeout=min(0.1, remaining)):
                    if self.callback_state.payload is None:
                        raise McpOAuthCallbackError(
                            "localhost OAuth callback completed without payload.",
                            server_id=self.server_id,
                            authorization_server_url=self.authorization_server_url,
                        )
                    return self.callback_state.payload
        except KeyboardInterrupt as exc:
            raise McpOAuthCallbackError(
                "OAuth login interrupted while waiting for the localhost callback.",
                server_id=self.server_id,
                authorization_server_url=self.authorization_server_url,
            ) from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


def _open_localhost_listener(
    *,
    server_id: str,
    authorization_server_url: str,
    redirect_host: str | None,
    redirect_port: int | None,
) -> _LocalhostCallbackListener:
    host = str(redirect_host or "127.0.0.1").strip() or "127.0.0.1"
    preferred_port = 0 if redirect_port is None else int(redirect_port)
    attempted_ports = [preferred_port, 0]
    last_error: OSError | None = None
    for index, port in enumerate(attempted_ports):
        try:
            return _LocalhostCallbackListener(
                server_id=server_id,
                authorization_server_url=authorization_server_url,
                host=host,
                port=port,
            )
        except OSError as exc:
            last_error = exc
            if index == len(attempted_ports) - 1:
                break
    raise McpOAuthCallbackError(
        f"failed to bind localhost OAuth callback listener on host '{host}' after two attempts.",
        server_id=server_id,
        authorization_server_url=authorization_server_url,
    ) from last_error


def _write_manual_login_instructions(
    writer: Callable[[str], None],
    *,
    authorization_url: str,
    redirect_uri: str,
) -> None:
    writer(
        "Open this URL in a browser to continue MCP OAuth login:\n"
        f"{authorization_url}\n"
        f"Redirect URI: {redirect_uri}\n"
        "This flow still requires localhost callback access and cannot complete without "
        "the redirect reaching this machine."
    )


def perform_authorization_code_login(
    *,
    server_id: str,
    authorization_server_metadata: McpAuthorizationServerMetadata,
    resource_server_url: str,
    client_id: str,
    scopes: tuple[str, ...] | list[str] | None = None,
    redirect_host: str | None = None,
    redirect_port: int | None = None,
    timeout_s: float = _LOCALHOST_CALLBACK_TIMEOUT_S,
    token_timeout_s: float = 10.0,
    browser_opener: Callable[[str], bool] | None = None,
    output_write: Callable[[str], None] | None = None,
    interrupt_check: Callable[[], None] | None = None,
) -> McpOAuthLoginResult:
    writer = output_write or print
    listener = _open_localhost_listener(
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.authorization_endpoint,
        redirect_host=redirect_host,
        redirect_port=redirect_port,
    )
    try:
        code_verifier = generate_pkce_verifier()
        code_challenge = build_pkce_challenge(code_verifier)
        state = generate_oauth_state()
        authorization_url = build_authorization_url(
            server_id=server_id,
            authorization_server_metadata=authorization_server_metadata,
            resource_server_url=resource_server_url,
            client_id=client_id,
            redirect_uri=listener.redirect_uri,
            code_challenge=code_challenge,
            state=state,
            scopes=scopes,
        )
        opener = browser_opener or webbrowser.open
        try:
            browser_opened = bool(opener(authorization_url))
        except Exception:
            browser_opened = False
        if not browser_opened:
            _write_manual_login_instructions(
                writer,
                authorization_url=authorization_url,
                redirect_uri=listener.redirect_uri,
            )
        callback = listener.wait_for_callback(timeout_s=timeout_s, interrupt_check=interrupt_check)
        if callback.error:
            details = callback.error
            if callback.error_description:
                details = f"{details}: {callback.error_description}"
            raise McpOAuthCallbackError(
                f"authorization server returned callback error '{details}'.",
                server_id=server_id,
                authorization_server_url=authorization_server_metadata.authorization_endpoint,
            )
        if callback.state != state:
            raise McpOAuthCallbackError(
                "received OAuth callback with mismatched state.",
                server_id=server_id,
                authorization_server_url=authorization_server_metadata.authorization_endpoint,
            )
        if not callback.code:
            raise McpOAuthCallbackError(
                "received OAuth callback without an authorization code.",
                server_id=server_id,
                authorization_server_url=authorization_server_metadata.authorization_endpoint,
            )
        record = exchange_authorization_code(
            server_id=server_id,
            authorization_server_metadata=authorization_server_metadata,
            resource_server_url=resource_server_url,
            client_id=client_id,
            code=callback.code,
            redirect_uri=listener.redirect_uri,
            code_verifier=code_verifier,
            requested_scopes=scopes,
            timeout_s=token_timeout_s,
        )
        save_oauth_token_record(server_id, record)
        return McpOAuthLoginResult(
            server_id=server_id,
            authorization_url=authorization_url,
            redirect_uri=listener.redirect_uri,
            browser_opened=browser_opened,
            token_record=record,
        )
    finally:
        listener.close()
