"""Production coordinator for IDE-owned MCP OAuth authorization-code login.

The coordinator is intentionally protocol-agnostic.  It starts a loopback
listener, returns public flow metadata immediately, and completes discovery,
callback validation, code exchange, and encrypted credential persistence
without placing authorization codes or tokens in a caller-visible object.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import math
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from ..mcp.oauth import (
    McpAuthorizationServerMetadata,
    canonical_mcp_resource_uri,
    discover_authorization_server_metadata,
    exchange_authorization_code,
)
from ..mcp.oauth_store import (
    McpOAuthTokenRecord,
    delete_oauth_token_record,
    load_oauth_token_record,
    save_oauth_token_record,
)
from .mcp_oauth_lifecycle import (
    AuthorizationCodeFlowRequest,
    McpOAuthFlowRegistry,
    OAuthCompletionClaim,
    OAuthCompletionRejectedError,
    OAuthFlowState,
    OAuthFlowStateError,
    OAuthFlowStatus,
    OAuthLogoutResult,
    OAuthTokenSet,
    OAuthTokenVault,
    OAuthValidationError,
)

__all__ = [
    "EncryptedMcpOAuthTokenVault",
    "McpOAuthCoordinatorConfig",
    "McpOAuthCoordinatorError",
    "McpOAuthIdeCoordinator",
    "McpOAuthLoginRequest",
]

_CALLBACK_PREFIX = "/oauth/callback/"
_MAX_CALLBACK_TARGET_BYTES = 16 * 1024
_MAX_AUTHORIZATION_CODE_BYTES = 8 * 1024
_MAX_STATE_BYTES = 2 * 1024
_TOKEN_VAULT_LOCK = threading.RLock()


class McpOAuthCoordinatorError(RuntimeError):
    """Coordinator error safe for logs and IDE protocol error messages."""


class _DiscoveryCallable(Protocol):
    def __call__(
        self,
        *,
        server_id: str,
        resource_server_url: str,
        authorization_server_url: str | None = None,
        timeout_s: float = 10.0,
    ) -> McpAuthorizationServerMetadata: ...


class _ExchangeCallable(Protocol):
    def __call__(
        self,
        *,
        server_id: str,
        authorization_server_metadata: McpAuthorizationServerMetadata,
        resource_server_url: str,
        client_id: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        requested_scopes: tuple[str, ...],
        timeout_s: float,
    ) -> McpOAuthTokenRecord: ...


@dataclass(frozen=True, slots=True)
class McpOAuthLoginRequest:
    server_id: str
    resource_server_url: str
    client_id: str = field(repr=False)
    scopes: tuple[str, ...] = field(default_factory=tuple)
    authorization_server_url: str | None = None


@dataclass(frozen=True, slots=True)
class McpOAuthCoordinatorConfig:
    callback_host: str = "127.0.0.1"
    callback_port: int = 0
    callback_timeout_seconds: float = 10 * 60.0
    discovery_timeout_seconds: float = 10.0
    token_timeout_seconds: float = 15.0
    shutdown_wait_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.callback_host != "127.0.0.1":
            raise OAuthValidationError("MCP OAuth callback host must be 127.0.0.1.")
        if (
            isinstance(self.callback_port, bool)
            or not isinstance(self.callback_port, int)
            or self.callback_port < 0
            or self.callback_port > 65535
            or 0 < self.callback_port < 1024
        ):
            raise OAuthValidationError("MCP OAuth callback port is invalid.")
        _bounded_seconds(
            self.callback_timeout_seconds,
            field_name="callback timeout",
            minimum=30.0,
            maximum=25 * 60.0,
        )
        _bounded_seconds(
            self.discovery_timeout_seconds,
            field_name="discovery timeout",
            minimum=1.0,
            maximum=60.0,
        )
        _bounded_seconds(
            self.token_timeout_seconds,
            field_name="token timeout",
            minimum=1.0,
            maximum=120.0,
        )
        _bounded_seconds(
            self.shutdown_wait_seconds,
            field_name="shutdown wait",
            minimum=0.0,
            maximum=120.0,
        )


class EncryptedMcpOAuthTokenVault(OAuthTokenVault):
    """Adapter from lifecycle credentials to Alysis Code's encrypted OAuth store."""

    def store(self, server_id: str, tokens: OAuthTokenSet, *, binding_id: str) -> None:
        if tokens.expires_at is None:
            raise OAuthValidationError("MCP OAuth token expiry is missing.")
        expires_at = datetime.fromtimestamp(float(tokens.expires_at), tz=UTC).replace(microsecond=0)
        obtained_at = datetime.now(UTC).replace(microsecond=0)
        record = McpOAuthTokenRecord(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_type=tokens.token_type,
            expires_at=expires_at,
            granted_scopes=tokens.scopes,
            obtained_at=obtained_at,
            binding_id=binding_id,
        )
        with _TOKEN_VAULT_LOCK:
            save_oauth_token_record(server_id, record)

    def load(self, server_id: str) -> OAuthTokenSet | None:
        with _TOKEN_VAULT_LOCK:
            record = load_oauth_token_record(server_id)
        if record is None:
            return None
        return _tokens_from_record(record)

    def has_binding(self, server_id: str, *, binding_id: str) -> bool:
        with _TOKEN_VAULT_LOCK:
            record = load_oauth_token_record(server_id)
        if record is None or record.binding_id is None:
            return False
        return hmac.compare_digest(record.binding_id, binding_id)

    def delete(self, server_id: str) -> bool:
        with _TOKEN_VAULT_LOCK:
            return delete_oauth_token_record(server_id)


@dataclass(frozen=True, slots=True)
class _CallbackPayload:
    code: str | None = field(repr=False)
    state: str | None = field(repr=False)
    provider_error: bool = False
    malformed: bool = False


class _CallbackInbox:
    def __init__(self) -> None:
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._payload: _CallbackPayload | None = None
        self._expected_state: str | None = None

    def expect_state(self, state: str) -> None:
        with self._lock:
            if self._expected_state is not None:
                raise McpOAuthCoordinatorError("MCP OAuth callback state is already configured.")
            self._expected_state = state

    def deliver(self, payload: _CallbackPayload) -> bool:
        with self._lock:
            if (
                self._expected_state is None
                or payload.state is None
                or not hmac.compare_digest(self._expected_state, payload.state)
            ):
                return False
            if self._payload is not None:
                return False
            self._payload = payload
            self.event.set()
            return True

    def read(self) -> _CallbackPayload | None:
        with self._lock:
            return self._payload


class _LoopbackHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _LoopbackCallbackHandler(BaseHTTPRequestHandler):
    server_version = "AlysisOAuthCallback/2.0"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        del format, args

    @property
    def callback_path(self) -> str:
        return str(self.server.callback_path)  # type: ignore[attr-defined]

    @property
    def inbox(self) -> _CallbackInbox:
        return self.server.callback_inbox  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Host", "") != str(
            self.server.expected_host_header  # type: ignore[attr-defined]
        ):
            self._send_page(HTTPStatus.BAD_REQUEST, succeeded=False)
            return
        if len(self.path.encode("utf-8", errors="ignore")) > _MAX_CALLBACK_TARGET_BYTES:
            self._send_page(HTTPStatus.REQUEST_URI_TOO_LONG, succeeded=False)
            return
        parsed = urlsplit(self.path)
        if parsed.path != self.callback_path:
            self._send_page(HTTPStatus.NOT_FOUND, succeeded=False)
            return
        try:
            query = parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=8,
            )
        except ValueError:
            self.inbox.deliver(_CallbackPayload(code=None, state=None, malformed=True))
            self._send_page(HTTPStatus.BAD_REQUEST, succeeded=False)
            return
        code = _single_callback_value(query, "code", maximum=_MAX_AUTHORIZATION_CODE_BYTES)
        state = _single_callback_value(query, "state", maximum=_MAX_STATE_BYTES)
        provider_error = "error" in query
        malformed = (not provider_error and code is None) or state is None
        accepted = self.inbox.deliver(
            _CallbackPayload(
                code=code,
                state=state,
                provider_error=provider_error,
                malformed=malformed,
            )
        )
        self._send_page(
            HTTPStatus.OK if accepted else HTTPStatus.CONFLICT,
            succeeded=accepted and not provider_error and not malformed,
        )

    def _send_page(self, status: HTTPStatus, *, succeeded: bool) -> None:
        message = (
            "Authorization received. Return to Visual Studio Code."
            if succeeded
            else "Authorization could not be accepted. Return to Visual Studio Code."
        )
        body = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Alysis Code OAuth</title></head><body><p>{html.escape(message)}</p>"
            "<script>window.history.replaceState({},document.title,window.location.pathname);"
            "</script></body></html>"
        ).encode()
        self.send_response(int(status))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


class _LoopbackCallbackListener:
    def __init__(self, *, host: str, port: int) -> None:
        self.callback_path = f"{_CALLBACK_PREFIX}{secrets.token_urlsafe(24)}"
        self.inbox = _CallbackInbox()
        try:
            self._server = _LoopbackHttpServer((host, port), _LoopbackCallbackHandler)
        except OSError as exc:
            raise McpOAuthCoordinatorError("MCP OAuth callback listener could not start.") from exc
        self._server.callback_path = self.callback_path  # type: ignore[attr-defined]
        self._server.callback_inbox = self.inbox  # type: ignore[attr-defined]
        bound_host, bound_port = self._server.server_address[:2]
        self._server.expected_host_header = f"{bound_host}:{bound_port}"  # type: ignore[attr-defined]
        self._closed = False
        self._close_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._serve,
            name="alysis-mcp-oauth-callback",
            daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        self._server.serve_forever(poll_interval=0.05)

    @property
    def redirect_uri(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{self.callback_path}"

    def expect_state(self, state: str) -> None:
        self.inbox.expect_state(state)

    def wait(
        self, *, timeout_seconds: float, cancel_event: threading.Event
    ) -> _CallbackPayload | None:
        deadline = time.monotonic() + timeout_seconds
        while not cancel_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self.inbox.event.wait(min(0.05, remaining)):
                return self.inbox.read()
        return None

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._server.shutdown()
            self._server.server_close()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)


@dataclass(slots=True)
class _ActiveFlow:
    server_id: str
    registry: McpOAuthFlowRegistry
    listener: _LoopbackCallbackListener
    cancel_event: threading.Event
    thread: threading.Thread | None = None


class McpOAuthIdeCoordinator:
    """Own asynchronous loopback authorization for one IDE bridge process."""

    def __init__(
        self,
        *,
        registry_path: str | Path | None = None,
        workspace_roots: Iterable[str | Path] = (),
        vault: OAuthTokenVault | None = None,
        config: McpOAuthCoordinatorConfig | None = None,
        discovery: _DiscoveryCallable = discover_authorization_server_metadata,
        exchanger: _ExchangeCallable = exchange_authorization_code,
        revoker: Callable[[OAuthTokenSet], None] | None = None,
    ) -> None:
        self.config = config or McpOAuthCoordinatorConfig()
        self.vault = vault or EncryptedMcpOAuthTokenVault()
        self._workspace_roots = tuple(workspace_roots)
        self._registry = McpOAuthFlowRegistry(
            self.vault,
            registry_path,
            workspace_roots=self._workspace_roots,
            completion_lease_seconds=min(
                10 * 60.0,
                max(30.0, self.config.token_timeout_seconds + 15.0),
            ),
        )
        self._discovery = discovery
        self._exchanger = exchanger
        self._revoker = revoker
        self._lock = threading.RLock()
        self._completion_lock = threading.RLock()
        self._active: dict[str, _ActiveFlow] = {}
        self._blocked_servers: set[str] = set()
        self._closed = False

    def start_authorization_code(self, request: McpOAuthLoginRequest) -> OAuthFlowStatus:
        with self._lock:
            if self._closed:
                raise McpOAuthCoordinatorError("MCP OAuth coordinator is closed.")

        resource_server_url = _secure_resource_url(request.resource_server_url)
        try:
            metadata = self._discovery(
                server_id=request.server_id,
                resource_server_url=resource_server_url,
                authorization_server_url=request.authorization_server_url,
                timeout_s=self.config.discovery_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - discovery providers may include sensitive details
            raise McpOAuthCoordinatorError("MCP OAuth metadata discovery failed.") from None

        listener = _LoopbackCallbackListener(
            host=self.config.callback_host,
            port=self.config.callback_port,
        )
        flow_registry = McpOAuthFlowRegistry(
            self.vault,
            self._registry.path,
            redirect_allowlist=(listener.redirect_uri,),
            workspace_roots=self._workspace_roots,
            completion_lease_seconds=min(
                10 * 60.0,
                max(30.0, self.config.token_timeout_seconds + 15.0),
            ),
        )
        try:
            status = flow_registry.start_authorization_code(
                AuthorizationCodeFlowRequest(
                    server_id=request.server_id,
                    authorization_endpoint=metadata.authorization_endpoint,
                    token_endpoint=metadata.token_endpoint,
                    client_id=request.client_id,
                    redirect_uri=listener.redirect_uri,
                    scopes=request.scopes,
                    extra_authorization_params={"resource": resource_server_url},
                    expires_in=(
                        self.config.callback_timeout_seconds
                        + self.config.token_timeout_seconds
                        + 30.0
                    ),
                    # MCP OAuth does not require an OpenID id_token.  State and
                    # PKCE remain mandatory; nonce is still sent when accepted.
                    require_nonce=False,
                )
            )
            listener.expect_state(_state_from_authorization_url(status.authorization_url))
        except Exception:
            listener.close()
            raise

        active = _ActiveFlow(
            server_id=status.server_id,
            registry=flow_registry,
            listener=listener,
            cancel_event=threading.Event(),
        )
        worker = threading.Thread(
            target=self._run_flow,
            args=(active, status.flow_id, metadata, resource_server_url),
            name=f"alysis-mcp-oauth-{status.flow_id[:8]}",
            daemon=True,
        )
        active.thread = worker
        with self._completion_lock:
            self._blocked_servers.discard(status.server_id)
        with self._lock:
            if self._closed:
                listener.close()
                flow_registry.cancel(status.flow_id)
                raise McpOAuthCoordinatorError("MCP OAuth coordinator is closed.")
            self._active[status.flow_id] = active
        try:
            worker.start()
        except Exception:
            with self._lock:
                self._active.pop(status.flow_id, None)
            listener.close()
            flow_registry.fail_pending(status.flow_id, error_code="worker_start_failed")
            raise McpOAuthCoordinatorError("MCP OAuth worker could not start.") from None
        return status

    def status(self, flow_id: str) -> OAuthFlowStatus:
        status = self._registry.status(flow_id)
        if status.state in {
            OAuthFlowState.COMPLETED,
            OAuthFlowState.CANCELLED,
            OAuthFlowState.FAILED,
            OAuthFlowState.EXPIRED,
        }:
            self._close_terminal_handle(flow_id)
        return status

    def cancel(self, flow_id: str) -> OAuthFlowStatus:
        with self._lock:
            active = self._active.get(flow_id)
        if active is not None:
            active.cancel_event.set()
            active.listener.close()
        status = self._registry.status(flow_id)
        if status.state is OAuthFlowState.PENDING:
            registry = active.registry if active is not None else self._registry
            return registry.cancel(flow_id)
        return status

    def logout(self, server_id: str) -> OAuthLogoutResult:
        with self._completion_lock:
            self._blocked_servers.add(server_id)
            with self._lock:
                active_items = [
                    (flow_id, active)
                    for flow_id, active in self._active.items()
                    if active.server_id == server_id
                ]
            for flow_id, active in active_items:
                active.cancel_event.set()
                active.listener.close()
                try:
                    status = active.registry.status(flow_id)
                    if status.state is OAuthFlowState.PENDING:
                        active.registry.cancel(flow_id)
                except Exception:  # noqa: BLE001 - logout continues local cleanup
                    pass
            return self._registry.logout(server_id, revoker=self._revoker)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active_items = list(self._active.items())
        with self._completion_lock:
            self._blocked_servers.update(active.server_id for _, active in active_items)
        for flow_id, active in active_items:
            active.cancel_event.set()
            active.listener.close()
            try:
                status = active.registry.status(flow_id)
                if status.state is OAuthFlowState.PENDING:
                    active.registry.cancel(flow_id)
            except Exception:  # noqa: BLE001 - shutdown is best effort after listener close
                pass
        deadline = time.monotonic() + self.config.shutdown_wait_seconds
        for _, active in active_items:
            thread = active.thread
            if thread is None or thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._active.clear()

    def __enter__(self) -> McpOAuthIdeCoordinator:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def _run_flow(
        self,
        active: _ActiveFlow,
        flow_id: str,
        metadata: McpAuthorizationServerMetadata,
        resource_server_url: str,
    ) -> None:
        claim: OAuthCompletionClaim | None = None
        try:
            payload = active.listener.wait(
                timeout_seconds=self.config.callback_timeout_seconds,
                cancel_event=active.cancel_event,
            )
            if active.cancel_event.is_set():
                self._cancel_pending_if_possible(active.registry, flow_id)
                return
            if payload is None:
                self._fail_pending_if_possible(active.registry, flow_id, "callback_timeout")
                return
            if payload.provider_error:
                self._fail_pending_if_possible(active.registry, flow_id, "provider_denied")
                return
            if payload.malformed or payload.code is None or payload.state is None:
                self._fail_pending_if_possible(active.registry, flow_id, "invalid_callback")
                return
            try:
                claim = active.registry.begin_completion(flow_id, state=payload.state)
            except OAuthCompletionRejectedError:
                self._fail_pending_if_possible(active.registry, flow_id, "callback_rejected")
                return
            if active.cancel_event.is_set():
                active.registry.fail_completion(claim, error_code="cancelled_by_user")
                return
            material = claim.material
            if material.pkce_verifier is None or material.redirect_uri is None:
                active.registry.fail_completion(claim, error_code="exchange_material_missing")
                return
            try:
                record = self._exchanger(
                    server_id=material.server_id,
                    authorization_server_metadata=metadata,
                    resource_server_url=resource_server_url,
                    client_id=material.client_id,
                    code=payload.code,
                    redirect_uri=material.redirect_uri,
                    code_verifier=material.pkce_verifier,
                    requested_scopes=material.scopes,
                    timeout_s=self.config.token_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - token/code details must not escape the worker
                active.registry.fail_completion(claim, error_code="token_exchange_failed")
                return
            with self._completion_lock:
                if active.cancel_event.is_set() or active.server_id in self._blocked_servers:
                    active.registry.fail_completion(claim, error_code="cancelled_by_user")
                    return
                active.registry.finish_completion(claim, _tokens_from_record(record))
        except OAuthFlowStateError:
            return
        except Exception:  # noqa: BLE001 - convert every worker failure to an allowlisted code
            if claim is not None:
                try:
                    active.registry.fail_completion(claim, error_code="authorization_worker_failed")
                except Exception:  # noqa: BLE001
                    pass
            else:
                self._fail_pending_if_possible(
                    active.registry, flow_id, "authorization_worker_failed"
                )
        finally:
            active.listener.close()
            with self._lock:
                if self._active.get(flow_id) is active:
                    self._active.pop(flow_id, None)

    def _close_terminal_handle(self, flow_id: str) -> None:
        with self._lock:
            active = self._active.get(flow_id)
        if active is not None:
            active.listener.close()

    @staticmethod
    def _fail_pending_if_possible(
        registry: McpOAuthFlowRegistry, flow_id: str, error_code: str
    ) -> None:
        try:
            if registry.status(flow_id).state is OAuthFlowState.PENDING:
                registry.fail_pending(flow_id, error_code=error_code)
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _cancel_pending_if_possible(registry: McpOAuthFlowRegistry, flow_id: str) -> None:
        try:
            if registry.status(flow_id).state is OAuthFlowState.PENDING:
                registry.cancel(flow_id)
        except Exception:  # noqa: BLE001
            return


def _tokens_from_record(record: McpOAuthTokenRecord) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=record.access_token,
        refresh_token=record.refresh_token,
        token_type=record.token_type,
        expires_at=record.expires_at.timestamp(),
        scopes=record.granted_scopes,
    )


def _single_callback_value(query: dict[str, list[str]], key: str, *, maximum: int) -> str | None:
    values = query.get(key)
    if values is None or len(values) != 1:
        return None
    value = values[0]
    if not value or len(value.encode("utf-8")) > maximum:
        return None
    return value


def _state_from_authorization_url(authorization_url: str | None) -> str:
    if authorization_url is None:
        raise McpOAuthCoordinatorError("MCP OAuth authorization URL is missing.")
    try:
        values = parse_qs(urlsplit(authorization_url).query, max_num_fields=32)["state"]
    except (KeyError, ValueError):
        raise McpOAuthCoordinatorError("MCP OAuth authorization state is missing.") from None
    if len(values) != 1 or not values[0]:
        raise McpOAuthCoordinatorError("MCP OAuth authorization state is invalid.")
    return values[0]


def _secure_resource_url(value: str) -> str:
    try:
        split = urlsplit(value)
        host = str(split.hostname or "")
        if not host or split.username is not None or split.password is not None or split.fragment:
            raise ValueError
        if split.scheme.lower() == "http":
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        elif split.scheme.lower() != "https":
            raise ValueError
        return canonical_mcp_resource_uri(value)
    except (TypeError, ValueError):
        raise OAuthValidationError(
            "MCP OAuth resource server must use HTTPS or loopback HTTP."
        ) from None


def _bounded_seconds(value: object, *, field_name: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
        or float(value) > maximum
    ):
        raise OAuthValidationError(f"MCP OAuth {field_name} is outside the allowed range.")
    return float(value)
