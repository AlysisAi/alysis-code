from __future__ import annotations

import codecs
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from .. import __version__
from .errors import (
    McpAuthError,
    McpProtocolError,
    McpRemoteError,
    McpTimeoutError,
    McpTransportError,
)
from .jsonrpc import (
    JsonRpcIdGenerator,
    JsonRpcNotification,
    JsonRpcProtocolError,
    JsonRpcRequest,
    JsonRpcResponse,
    build_jsonrpc_notification,
    build_jsonrpc_request,
    build_jsonrpc_result_response,
    encode_jsonrpc_message,
    parse_jsonrpc_line,
)
from .models import ResolvedMcpServer
from .oauth import (
    McpAuthorizationServerMetadata,
    McpOAuthAuthRequiredError,
    McpOAuthDiscoveryError,
    McpOAuthError,
    McpOAuthInsufficientScopeError,
    McpOAuthReLoginRequired,
    McpOAuthTokenExchangeError,
    discover_authorization_server_metadata,
    is_token_expired,
    parse_www_authenticate_bearer_challenge,
    refresh_access_token,
    resolve_requested_scopes,
)
from .oauth_store import (
    McpOAuthTokenRecord,
    McpOAuthTokenStoreError,
    delete_oauth_token_record,
    load_oauth_token_record,
    save_oauth_token_record,
)
from .server_requests import (
    McpServerRequestContext,
    McpServerRequestHandler,
    McpServerRequestHandlerError,
    McpUnsupportedServerRequestError,
)

_HTTP_ACCEPT_HEADER = "application/json, text/event-stream"
_USER_AGENT = f"alysis-code/{__version__}"
_SAFE_ERROR_BODY_CHAR_LIMIT = 1000
_OPEN_ENDED_SSE_FOLLOW_UP_QUIET_WINDOW_S = 0.1
_OPEN_ENDED_SSE_READER_JOIN_TIMEOUT_S = 1.0


class McpHttpTransportError(McpTransportError):
    pass


class McpHttpTransportTimeoutError(McpHttpTransportError, McpTimeoutError):
    pass


class McpHttpTransportProtocolError(McpHttpTransportError, McpProtocolError):
    error_code = "mcp_http_transport_protocol_error"
    retryable = False


class McpHttpTransportRemoteError(McpHttpTransportError, McpRemoteError):
    error_code = "mcp_http_transport_remote_error"
    retryable = False


class McpHttpTransportAuthRequiredError(McpHttpTransportError, McpAuthError):
    error_code = "mcp_http_transport_auth_required"
    retryable = False


class McpHttpTransportSessionExpiredError(McpHttpTransportError):
    error_code = "mcp_http_transport_session_expired"
    retryable = False


@dataclass(frozen=True)
class _SseEvent:
    event: str | None
    event_id: str | None
    retry_ms: int | None
    data: str


@dataclass(frozen=True)
class _SseReaderItem:
    kind: str
    activity_sequence: int
    activity_monotonic: float
    event: _SseEvent | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _SseReaderSnapshot:
    activity_sequence: int
    activity_monotonic: float
    event_in_progress: bool


class _SseReaderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest_activity_sequence = 0
        self._latest_activity_monotonic = 0.0
        self._event_in_progress = False

    def record_activity(self, *, event_in_progress: bool) -> tuple[int, float]:
        with self._lock:
            self._latest_activity_sequence += 1
            self._latest_activity_monotonic = time.monotonic()
            self._event_in_progress = event_in_progress
            return self._latest_activity_sequence, self._latest_activity_monotonic

    def set_event_in_progress(self, *, event_in_progress: bool) -> None:
        with self._lock:
            self._event_in_progress = event_in_progress

    def snapshot(self) -> _SseReaderSnapshot:
        with self._lock:
            return _SseReaderSnapshot(
                activity_sequence=self._latest_activity_sequence,
                activity_monotonic=self._latest_activity_monotonic,
                event_in_progress=self._event_in_progress,
            )


class _SseEventParser:
    def __init__(self) -> None:
        self._event_name: str | None = None
        self._event_id: str | None = None
        self._retry_ms: int | None = None
        self._data_lines: list[str] = []
        self._saw_event_field = False

    def event_in_progress(self, *, has_partial_line: bool) -> bool:
        return (
            has_partial_line
            or self._saw_event_field
            or self._event_name is not None
            or self._event_id is not None
            or self._retry_ms is not None
            or bool(self._data_lines)
        )

    def _reset(self) -> None:
        self._event_name = None
        self._event_id = None
        self._retry_ms = None
        self._data_lines = []
        self._saw_event_field = False

    def _flush_event(self) -> _SseEvent | None:
        if (
            not self._saw_event_field
            and self._event_name is None
            and self._event_id is None
            and self._retry_ms is None
            and not self._data_lines
        ):
            return None
        if (
            not self._data_lines
            and self._event_name is None
            and self._event_id is None
            and self._retry_ms is None
        ):
            self._reset()
            return None
        event = _SseEvent(
            event=self._event_name,
            event_id=self._event_id,
            retry_ms=self._retry_ms,
            data="\n".join(self._data_lines),
        )
        self._reset()
        return event

    def feed_line(self, line: str) -> _SseEvent | None:
        if line == "":
            return self._flush_event()
        if line.startswith(":"):
            return None
        self._saw_event_field = True
        field_name, sep, field_value = line.partition(":")
        if not sep:
            field_value = ""
        elif field_value.startswith(" "):
            field_value = field_value[1:]
        if field_name == "data":
            self._data_lines.append(field_value)
            return None
        if field_name == "event":
            self._event_name = field_value
            return None
        if field_name == "id":
            self._event_id = field_value
            return None
        if field_name == "retry" and field_value.isdigit():
            self._retry_ms = int(field_value)
        return None

    def finish_eof(self) -> _SseEvent | None:
        return self._flush_event()


def _extract_sse_line(text_buffer: str, *, eof: bool = False) -> tuple[str | None, str]:
    for index, character in enumerate(text_buffer):
        if character == "\n":
            line = text_buffer[:index]
            if line.endswith("\r"):
                line = line[:-1]
            return line, text_buffer[index + 1 :]
        if character == "\r":
            if index + 1 < len(text_buffer) and text_buffer[index + 1] == "\n":
                return text_buffer[:index], text_buffer[index + 2 :]
            if index + 1 == len(text_buffer) and not eof:
                return None, text_buffer
            return text_buffer[:index], text_buffer[index + 1 :]
    return None, text_buffer


def _redacted_url(value: str) -> str:
    split = urlsplit(value)
    hostname = split.hostname or ""
    if split.port is None:
        netloc = hostname
    else:
        netloc = f"{hostname}:{split.port}"
    redacted = SplitResult(
        scheme=split.scheme,
        netloc=netloc,
        path=split.path or "/",
        query="",
        fragment="",
    )
    return urlunsplit(redacted)


def _normalized_content_type(raw_value: str | None) -> str:
    value = str(raw_value or "").strip().lower()
    if not value:
        return ""
    return value.split(";", 1)[0].strip()


def _response_body_snippet(response: httpx.Response) -> str:
    try:
        response.read()
    except Exception:
        return "<unable to read response body>"
    try:
        body = response.text
    except Exception:
        return "<unable to decode response body>"
    if len(body) > _SAFE_ERROR_BODY_CHAR_LIMIT:
        body = body[:_SAFE_ERROR_BODY_CHAR_LIMIT] + "...(truncated)"
    return body


def _format_http_error(
    *,
    server: ResolvedMcpServer,
    message: str,
) -> str:
    return f"MCP HTTP server '{server.id}' (url '{_redacted_url(server.url or '')}'): {message}"


def _iter_sse_events(response: httpx.Response) -> Iterator[_SseEvent]:
    parser = _SseEventParser()
    try:
        raw_lines = response.iter_lines()
    except Exception as exc:  # noqa: BLE001
        raise McpHttpTransportProtocolError("failed to read HTTP event stream") from exc

    for raw_line in raw_lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8")
        else:
            line = str(raw_line)
        event = parser.feed_line(line)
        if event is not None:
            yield event
    event = parser.finish_eof()
    if event is not None:
        yield event


class McpHttpTransport:
    def __init__(
        self,
        *,
        server: ResolvedMcpServer,
        workspace_root: Path,
        client: httpx.Client | None = None,
        server_request_context: McpServerRequestContext | None = None,
        server_request_handler: McpServerRequestHandler | None = None,
    ) -> None:
        if server.transport != "http":
            raise McpHttpTransportError(f"MCP server '{server.id}' is not an HTTP transport.")
        if server.url is None:
            raise McpHttpTransportError(f"MCP server '{server.id}' is missing an HTTP URL.")
        self.server = server
        self.workspace_root = workspace_root.resolve()
        self._server_request_context = server_request_context
        self._server_request_handler = server_request_handler
        self._id_generator = JsonRpcIdGenerator()
        self._notifications: queue.Queue[JsonRpcNotification] = queue.Queue()
        self._client = client or httpx.Client(follow_redirects=False)
        self._owns_client = client is None
        self._closed = False
        self._session_id: str | None = None
        self._negotiated_protocol_version: str | None = None
        self._oauth_metadata: McpAuthorizationServerMetadata | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def session_negotiated(self) -> bool:
        return bool(self._session_id)

    @property
    def negotiated_protocol_version(self) -> str | None:
        return self._negotiated_protocol_version

    def stderr_tail(self) -> str:
        return ""

    def reset_session_state(self) -> None:
        self._session_id = None
        self._negotiated_protocol_version = None

    def set_negotiated_protocol_version(self, protocol_version: str | None) -> None:
        cleaned = str(protocol_version or "").strip()
        self._negotiated_protocol_version = cleaned or None

    def drain_notifications(self) -> tuple[JsonRpcNotification, ...]:
        notifications: list[JsonRpcNotification] = []
        while True:
            try:
                notifications.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return tuple(notifications)

    def _build_headers(self, *, access_token: str | None = None) -> dict[str, str]:
        headers = dict(self.server.headers)
        headers["Accept"] = _HTTP_ACCEPT_HEADER
        headers["Content-Type"] = "application/json"
        headers["User-Agent"] = _USER_AGENT
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        if self._negotiated_protocol_version:
            headers["MCP-Protocol-Version"] = self._negotiated_protocol_version
        return headers

    def _build_error(
        self,
        message: str,
        *,
        exc_type: type[McpHttpTransportError] = McpHttpTransportError,
    ) -> McpHttpTransportError:
        return exc_type(_format_http_error(server=self.server, message=message))

    def _oauth_enabled(self) -> bool:
        return self.server.oauth is not None

    def _oauth_authorization_server_hint(self) -> str | None:
        if self._oauth_metadata is not None:
            return self._oauth_metadata.issuer or self._oauth_metadata.token_endpoint
        if self.server.oauth is not None and self.server.oauth.authorization_server_url:
            return self.server.oauth.authorization_server_url
        return None

    def _clear_stored_oauth_tokens(self) -> None:
        try:
            delete_oauth_token_record(self.server.id)
        except McpOAuthTokenStoreError:
            return

    def _discover_oauth_metadata(
        self,
        *,
        unauthorized_response: httpx.Response | None = None,
    ) -> McpAuthorizationServerMetadata:
        if self._oauth_metadata is not None:
            return self._oauth_metadata
        oauth_config = self.server.oauth
        if oauth_config is None:
            raise self._build_error("OAuth is not configured for this HTTP MCP server.")
        metadata = discover_authorization_server_metadata(
            server_id=self.server.id,
            resource_server_url=self.server.url or "",
            unauthorized_response=unauthorized_response,
            authorization_server_url=oauth_config.authorization_server_url,
            timeout_s=min(self.server.call_timeout_s, 10.0),
            client=self._client,
        )
        self._oauth_metadata = metadata
        return metadata

    def _refresh_oauth_token_record(
        self,
        *,
        current_record: McpOAuthTokenRecord,
        unauthorized_response: httpx.Response | None = None,
    ) -> McpOAuthTokenRecord:
        metadata: McpAuthorizationServerMetadata | None = None
        try:
            metadata = self._discover_oauth_metadata(unauthorized_response=unauthorized_response)
            oauth_config = self.server.oauth
            if oauth_config is None:
                raise McpOAuthReLoginRequired(
                    "OAuth configuration is missing for this HTTP MCP server.",
                    server_id=self.server.id,
                    authorization_server_url=self._oauth_authorization_server_hint(),
                )
            resolved_scopes = resolve_requested_scopes(
                configured_scopes=oauth_config.scopes,
                challenge_scope=None,
                metadata_scopes_supported=None,
                existing_granted_scopes=current_record.granted_scopes,
                purpose="refresh",
            )
            refreshed = refresh_access_token(
                server_id=self.server.id,
                authorization_server_metadata=metadata,
                resource_server_url=self.server.url or "",
                client_id=oauth_config.client_id,
                refresh_token=current_record.refresh_token or "",
                requested_scopes=resolved_scopes,
                existing_granted_scopes=current_record.granted_scopes,
                timeout_s=min(self.server.call_timeout_s, 10.0),
                client=self._client,
            )
            save_oauth_token_record(self.server.id, refreshed)
            return refreshed
        except (McpOAuthDiscoveryError, McpOAuthTokenExchangeError, McpOAuthTokenStoreError) as exc:
            self._clear_stored_oauth_tokens()
            raise McpOAuthReLoginRequired(
                f"stored OAuth credentials could not be refreshed. Run 'alysis mcp auth login {self.server.id}' again.",
                server_id=self.server.id,
                authorization_server_url=(
                    metadata.token_endpoint
                    if metadata is not None
                    else self._oauth_authorization_server_hint()
                ),
            ) from exc

    def _resolve_oauth_token_record(self, *, method: str) -> McpOAuthTokenRecord:
        try:
            record = load_oauth_token_record(self.server.id)
        except McpOAuthTokenStoreError as exc:
            raise McpOAuthReLoginRequired(
                f"failed to read stored OAuth credentials before HTTP MCP '{method}'. Run 'alysis mcp auth login {self.server.id}' again.",
                server_id=self.server.id,
                authorization_server_url=self._oauth_authorization_server_hint(),
            ) from exc
        if record is None:
            raise McpOAuthAuthRequiredError(
                f"OAuth login is required before HTTP MCP '{method}'. Run 'alysis mcp auth login {self.server.id}'.",
                server_id=self.server.id,
                authorization_server_url=self._oauth_authorization_server_hint(),
            )
        if not is_token_expired(record):
            return record
        if not record.refresh_token:
            self._clear_stored_oauth_tokens()
            raise McpOAuthReLoginRequired(
                f"stored OAuth credentials expired before HTTP MCP '{method}' and cannot be refreshed. Run 'alysis mcp auth login {self.server.id}' again.",
                server_id=self.server.id,
                authorization_server_url=self._oauth_authorization_server_hint(),
            )
        return self._refresh_oauth_token_record(current_record=record)

    def _oauth_error_signal(self, response: httpx.Response) -> str | None:
        params = parse_www_authenticate_bearer_challenge(response.headers.get("WWW-Authenticate"))
        challenge_error = str(params.get("error") or "").strip()
        if challenge_error:
            return challenge_error
        try:
            payload = response.json()
        except Exception:
            return None
        if isinstance(payload, dict):
            body_error = payload.get("error")
            if isinstance(body_error, str):
                cleaned = body_error.strip()
                if cleaned:
                    return cleaned
        return None

    def _oauth_scope_hint(self, response: httpx.Response) -> str:
        params = parse_www_authenticate_bearer_challenge(response.headers.get("WWW-Authenticate"))
        challenge_scope = str(params.get("scope") or "").strip()
        resolved_scopes = resolve_requested_scopes(
            configured_scopes=None,
            challenge_scope=challenge_scope,
            metadata_scopes_supported=None,
            existing_granted_scopes=None,
            purpose="login",
        )
        if not resolved_scopes:
            return ""
        return f" Server requested scopes: {', '.join(resolved_scopes)}."

    def _handle_oauth_http_status(
        self,
        response: httpx.Response,
        *,
        method: str,
        oauth_record: McpOAuthTokenRecord | None,
        allow_request_replay: bool,
        oauth_retry_attempted: bool,
    ) -> bool:
        if (
            response.status_code == 403
            and self._oauth_error_signal(response) == "insufficient_scope"
        ):
            raise McpOAuthInsufficientScopeError(
                f"HTTP MCP '{method}' failed with insufficient_scope.",
                server_id=self.server.id,
                authorization_server_url=self._oauth_authorization_server_hint(),
            )
        if response.status_code != 401:
            return False
        scope_hint = self._oauth_scope_hint(response)
        if oauth_record is None:
            raise McpOAuthAuthRequiredError(
                f"OAuth login is required before HTTP MCP '{method}'.{scope_hint} Run 'alysis mcp auth login {self.server.id}'.",
                server_id=self.server.id,
                authorization_server_url=self._oauth_authorization_server_hint(),
            )
        if not oauth_record.refresh_token:
            self._clear_stored_oauth_tokens()
            raise McpOAuthReLoginRequired(
                f"HTTP MCP '{method}' returned 401 and stored OAuth credentials cannot be refreshed.{scope_hint} Run 'alysis mcp auth login {self.server.id}' again.",
                server_id=self.server.id,
                authorization_server_url=self._oauth_authorization_server_hint(),
            )
        if allow_request_replay:
            if oauth_retry_attempted:
                self._clear_stored_oauth_tokens()
                raise McpOAuthReLoginRequired(
                    f"HTTP MCP '{method}' still returned 401 after one OAuth refresh and retry.{scope_hint} Run 'alysis mcp auth login {self.server.id}' again.",
                    server_id=self.server.id,
                    authorization_server_url=self._oauth_authorization_server_hint(),
                )
            self._refresh_oauth_token_record(
                current_record=oauth_record,
                unauthorized_response=response,
            )
            return True
        self._refresh_oauth_token_record(
            current_record=oauth_record,
            unauthorized_response=response,
        )
        raise McpOAuthAuthRequiredError(
            f"HTTP MCP '{method}' returned 401.{scope_hint} OAuth credentials were refreshed, but the active request was not automatically retried because it may be side-effectful. Retry the request manually.",
            server_id=self.server.id,
            authorization_server_url=self._oauth_authorization_server_hint(),
        )

    def _capture_session_headers(self, response: httpx.Response, *, method: str) -> None:
        if method != "initialize":
            return
        session_id = str(response.headers.get("MCP-Session-Id") or "").strip()
        if session_id:
            self._session_id = session_id

    def _raise_for_http_status(
        self,
        response: httpx.Response,
        *,
        method: str,
        had_session_id: bool,
    ) -> None:
        status_code = response.status_code
        if 200 <= status_code < 300:
            return
        body_snippet = _response_body_snippet(response).strip()
        body_suffix = f": {body_snippet}" if body_snippet else ""
        if status_code == 401:
            raise self._build_error(
                "server requires HTTP MCP authentication. Configure static headers for this "
                "server, or use 'alysis mcp auth login <server_id>' for OAuth-enabled "
                f"HTTP MCP servers{body_suffix}",
                exc_type=McpHttpTransportAuthRequiredError,
            )
        if status_code == 404 and had_session_id:
            raise self._build_error(
                f"HTTP MCP session expired during '{method}'",
                exc_type=McpHttpTransportSessionExpiredError,
            )
        if method == "initialize" and not had_session_id and status_code in {400, 404, 405}:
            raise self._build_error(
                "initialize POST was rejected by the HTTP MCP server. The server may require "
                "legacy HTTP+SSE fallback behavior, which is intentionally out of scope in this "
                f"slice{body_suffix}",
                exc_type=McpHttpTransportProtocolError,
            )
        if status_code >= 500:
            raise self._build_error(
                f"HTTP MCP server returned {status_code}{body_suffix}",
                exc_type=McpHttpTransportRemoteError,
            )
        raise self._build_error(
            f"HTTP MCP request '{method}' failed with status {status_code}{body_suffix}",
            exc_type=McpHttpTransportProtocolError,
        )

    def _process_jsonrpc_message(
        self,
        message: JsonRpcNotification | JsonRpcRequest | JsonRpcResponse,
        *,
        method: str,
        expected_response_id: int | str | None,
        response_received: bool,
        server_request_timeout_s: float | None = None,
        allow_server_requests: bool = False,
    ) -> tuple[JsonRpcResponse | None, bool]:
        if isinstance(message, JsonRpcNotification):
            if response_received:
                raise self._build_error(
                    "received an additional JSON-RPC message after the matching HTTP response "
                    f"for '{method}'",
                    exc_type=McpHttpTransportProtocolError,
                )
            self._notifications.put(message)
            return None, response_received
        if isinstance(message, JsonRpcRequest):
            if not allow_server_requests:
                if response_received:
                    raise self._build_error(
                        f"received unsupported server-initiated request '{message.method}' during "
                        f"HTTP MCP '{method}' follow-up",
                        exc_type=McpHttpTransportProtocolError,
                    )
                raise self._build_error(
                    f"received unsupported server-initiated request '{message.method}' during "
                    f"HTTP MCP '{method}'",
                    exc_type=McpHttpTransportProtocolError,
                )
            self._handle_server_request_message(
                message,
                method=method,
                timeout_s=server_request_timeout_s,
            )
            return None, response_received
        if expected_response_id is None:
            raise self._build_error(
                f"received an unexpected JSON-RPC response while handling HTTP notification "
                f"'{method}'",
                exc_type=McpHttpTransportProtocolError,
            )
        if response_received:
            raise self._build_error(
                f"received multiple JSON-RPC responses for one HTTP MCP '{method}' request",
                exc_type=McpHttpTransportProtocolError,
            )
        if message.id != expected_response_id:
            raise self._build_error(
                f"received JSON-RPC response id {message.id!r} while waiting for "
                f"{expected_response_id!r} during HTTP MCP '{method}'",
                exc_type=McpHttpTransportProtocolError,
            )
        return message, True

    def _post_server_request_response(
        self,
        *,
        payload: dict[str, Any],
        request_method: str,
        timeout_s: float,
    ) -> None:
        try:
            self._send_jsonrpc_message(
                payload=payload,
                method=f"{request_method} response",
                timeout_s=timeout_s,
                expected_response_id=None,
            )
        except McpHttpTransportError as exc:
            raise self._build_error(
                f"failed to POST JSON-RPC response for server-initiated request '{request_method}'",
                exc_type=type(exc),
            ) from exc

    def _handle_server_request_message(
        self,
        message: JsonRpcRequest,
        *,
        method: str,
        timeout_s: float | None,
    ) -> None:
        if self._server_request_handler is None or self._server_request_context is None:
            raise self._build_error(
                f"received unsupported server-initiated request '{message.method}' during "
                f"HTTP MCP '{method}'",
                exc_type=McpHttpTransportProtocolError,
            )
        request_timeout_s = timeout_s or self.server.call_timeout_s
        try:
            result = self._server_request_handler.handle_request(
                context=self._server_request_context,
                method=message.method,
                request_id=message.id,
                params=message.params,
            )
        except McpUnsupportedServerRequestError:
            raise self._build_error(
                f"received unsupported server-initiated request '{message.method}' during "
                f"HTTP MCP '{method}'",
                exc_type=McpHttpTransportProtocolError,
            ) from None
        except McpServerRequestHandlerError as exc:
            self._post_server_request_response(
                payload={
                    "jsonrpc": "2.0",
                    "id": message.id,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    },
                },
                request_method=message.method,
                timeout_s=request_timeout_s,
            )
            raise self._build_error(
                f"failed to handle server-initiated request '{message.method}' during "
                f"HTTP MCP '{method}'",
                exc_type=McpHttpTransportProtocolError,
            ) from exc
        except Exception as exc:
            self._post_server_request_response(
                payload={
                    "jsonrpc": "2.0",
                    "id": message.id,
                    "error": {
                        "code": -32603,
                        "message": (
                            "Internal MCP host error while handling server-initiated request."
                        ),
                    },
                },
                request_method=message.method,
                timeout_s=request_timeout_s,
            )
            raise self._build_error(
                f"failed to handle server-initiated request '{message.method}' during "
                f"HTTP MCP '{method}'",
                exc_type=McpHttpTransportProtocolError,
            ) from exc
        self._post_server_request_response(
            payload=build_jsonrpc_result_response(
                request_id=message.id,
                result=result,
            ),
            request_method=message.method,
            timeout_s=request_timeout_s,
        )

    def _parse_single_jsonrpc_payload(
        self,
        raw_text: str,
        *,
        method: str,
    ) -> JsonRpcNotification | JsonRpcRequest | JsonRpcResponse:
        try:
            messages = parse_jsonrpc_line(raw_text)
        except JsonRpcProtocolError as exc:
            raise self._build_error(
                f"malformed JSON-RPC payload during HTTP MCP '{method}': {exc}",
                exc_type=McpHttpTransportProtocolError,
            ) from exc
        if len(messages) != 1:
            raise self._build_error(
                f"JSON-RPC batch payloads are not supported for HTTP MCP '{method}'",
                exc_type=McpHttpTransportProtocolError,
            )
        return messages[0]

    def _handle_application_json(
        self,
        response: httpx.Response,
        *,
        method: str,
        expected_response_id: int | str | None,
    ) -> JsonRpcResponse | None:
        try:
            response.read()
            raw_text = response.text
        except Exception as exc:  # noqa: BLE001
            raise self._build_error(
                f"failed to decode HTTP response body for '{method}'",
                exc_type=McpHttpTransportProtocolError,
            ) from exc
        if not raw_text.strip():
            if expected_response_id is None:
                return None
            raise self._build_error(
                f"HTTP MCP '{method}' returned an empty JSON body",
                exc_type=McpHttpTransportProtocolError,
            )
        message = self._parse_single_jsonrpc_payload(raw_text, method=method)
        parsed_response, response_received = self._process_jsonrpc_message(
            message,
            method=method,
            expected_response_id=expected_response_id,
            response_received=False,
            server_request_timeout_s=None,
            allow_server_requests=False,
        )
        if expected_response_id is None:
            return None
        if not response_received or parsed_response is None:
            raise self._build_error(
                f"HTTP MCP '{method}' completed without the matching JSON-RPC response",
                exc_type=McpHttpTransportProtocolError,
            )
        return parsed_response

    def _raise_sse_reader_error(
        self,
        *,
        method: str,
        timeout_s: float,
        error: BaseException,
    ) -> None:
        if isinstance(error, McpHttpTransportError):
            raise error
        if isinstance(error, httpx.TimeoutException):
            raise self._build_error(
                f"HTTP MCP '{method}' timed out after {timeout_s:.3f}s",
                exc_type=McpHttpTransportTimeoutError,
            ) from error
        if isinstance(error, httpx.HTTPError):
            raise self._build_error(
                f"HTTP MCP '{method}' event stream failed: {error}",
                exc_type=McpHttpTransportError,
            ) from error
        raise self._build_error(
            f"failed to read HTTP event stream for '{method}': {error}",
            exc_type=McpHttpTransportError,
        ) from error

    def _start_sse_reader(
        self,
        response: httpx.Response,
    ) -> tuple[queue.Queue[_SseReaderItem], threading.Thread, _SseReaderState]:
        reader_items: queue.Queue[_SseReaderItem] = queue.Queue()
        reader_state = _SseReaderState()

        def _reader() -> None:
            parser = _SseEventParser()
            decoder = codecs.getincrementaldecoder("utf-8")()
            text_buffer = ""

            def _queue_event(
                *,
                event: _SseEvent,
                activity_sequence: int,
                activity_monotonic: float,
            ) -> None:
                reader_items.put(
                    _SseReaderItem(
                        kind="event",
                        activity_sequence=activity_sequence,
                        activity_monotonic=activity_monotonic,
                        event=event,
                    )
                )

            try:
                try:
                    body_chunks = response.iter_bytes()
                except Exception as exc:  # noqa: BLE001
                    raise McpHttpTransportProtocolError("failed to read HTTP event stream") from exc
                for body_chunk in body_chunks:
                    if not body_chunk:
                        continue
                    activity_sequence, activity_monotonic = reader_state.record_activity(
                        event_in_progress=True
                    )
                    try:
                        # Keep HTTP content decoding enabled for POST-SSE so compressed
                        # event streams from servers or intermediaries still work.
                        text_buffer += decoder.decode(body_chunk)
                    except UnicodeDecodeError as exc:
                        raise McpHttpTransportProtocolError(
                            "failed to decode HTTP event stream as UTF-8"
                        ) from exc
                    while True:
                        line, text_buffer = _extract_sse_line(text_buffer)
                        if line is None:
                            break
                        event = parser.feed_line(line)
                        reader_state.set_event_in_progress(
                            event_in_progress=parser.event_in_progress(
                                has_partial_line=bool(text_buffer)
                            )
                        )
                        if event is not None:
                            _queue_event(
                                event=event,
                                activity_sequence=activity_sequence,
                                activity_monotonic=activity_monotonic,
                            )
                    reader_state.set_event_in_progress(
                        event_in_progress=parser.event_in_progress(
                            has_partial_line=bool(text_buffer)
                        )
                    )
                try:
                    text_buffer += decoder.decode(b"", final=True)
                except UnicodeDecodeError as exc:
                    raise McpHttpTransportProtocolError(
                        "failed to decode HTTP event stream as UTF-8"
                    ) from exc
                eof_activity_sequence = 0
                eof_activity_monotonic = 0.0
                if text_buffer:
                    eof_activity_sequence, eof_activity_monotonic = reader_state.record_activity(
                        event_in_progress=True
                    )
                    while True:
                        line, text_buffer = _extract_sse_line(text_buffer, eof=True)
                        if line is None:
                            break
                        event = parser.feed_line(line)
                        if event is not None:
                            _queue_event(
                                event=event,
                                activity_sequence=eof_activity_sequence,
                                activity_monotonic=eof_activity_monotonic,
                            )
                    if text_buffer:
                        event = parser.feed_line(text_buffer)
                        text_buffer = ""
                        if event is not None:
                            _queue_event(
                                event=event,
                                activity_sequence=eof_activity_sequence,
                                activity_monotonic=eof_activity_monotonic,
                            )
                final_event = parser.finish_eof()
                if final_event is not None:
                    if eof_activity_sequence == 0:
                        eof_activity_sequence, eof_activity_monotonic = (
                            reader_state.record_activity(event_in_progress=False)
                        )
                    else:
                        reader_state.set_event_in_progress(event_in_progress=False)
                    _queue_event(
                        event=final_event,
                        activity_sequence=eof_activity_sequence,
                        activity_monotonic=eof_activity_monotonic,
                    )
            except BaseException as exc:  # noqa: BLE001
                activity_sequence, activity_monotonic = reader_state.record_activity(
                    event_in_progress=False
                )
                reader_items.put(
                    _SseReaderItem(
                        kind="error",
                        activity_sequence=activity_sequence,
                        activity_monotonic=activity_monotonic,
                        error=exc,
                    )
                )
                return
            activity_sequence, activity_monotonic = reader_state.record_activity(
                event_in_progress=False
            )
            reader_items.put(
                _SseReaderItem(
                    kind="closed",
                    activity_sequence=activity_sequence,
                    activity_monotonic=activity_monotonic,
                )
            )

        reader_thread = threading.Thread(
            target=_reader,
            name=f"mcp-http-sse-{self.server.id}",
            daemon=True,
        )
        reader_thread.start()
        return reader_items, reader_thread, reader_state

    def _handle_open_ended_follow_up_message(
        self,
        message: JsonRpcNotification | JsonRpcRequest | JsonRpcResponse,
        *,
        method: str,
    ) -> None:
        if isinstance(message, JsonRpcNotification):
            self._notifications.put(message)
            return
        if isinstance(message, JsonRpcRequest):
            raise self._build_error(
                f"received unsupported server-initiated request '{message.method}' during "
                f"HTTP MCP '{method}' follow-up",
                exc_type=McpHttpTransportProtocolError,
            )
        raise self._build_error(
            "received an additional JSON-RPC message after the matching HTTP response "
            f"for '{method}'",
            exc_type=McpHttpTransportProtocolError,
        )

    def _handle_open_ended_request_event_stream(
        self,
        response: httpx.Response,
        *,
        method: str,
        expected_response_id: int | str,
        timeout_s: float,
    ) -> JsonRpcResponse:
        response_deadline = time.monotonic() + timeout_s
        last_reader_activity_monotonic: float | None = None
        last_observed_activity_sequence = 0
        reader_event_in_progress = False
        parsed_response: JsonRpcResponse | None = None
        reader_items, reader_thread, reader_state = self._start_sse_reader(response)
        try:
            while True:
                now = time.monotonic()
                if parsed_response is None:
                    wait_timeout = response_deadline - now
                    if wait_timeout <= 0:
                        raise self._build_error(
                            f"HTTP MCP '{method}' timed out after {timeout_s:.3f}s",
                            exc_type=McpHttpTransportTimeoutError,
                        )
                else:
                    snapshot = reader_state.snapshot()
                    if snapshot.activity_sequence > last_observed_activity_sequence:
                        last_observed_activity_sequence = snapshot.activity_sequence
                        last_reader_activity_monotonic = snapshot.activity_monotonic
                    reader_event_in_progress = snapshot.event_in_progress
                    assert last_reader_activity_monotonic is not None
                    if now >= response_deadline:
                        raise self._build_error(
                            f"HTTP MCP '{method}' timed out after {timeout_s:.3f}s",
                            exc_type=McpHttpTransportTimeoutError,
                        )
                    # Open-ended POST-SSE success is gated by raw reader progress, not only by
                    # completed events. A partially read trailing SSE event keeps follow-up active
                    # until that event either completes or the request deadline expires.
                    quiet_deadline = (
                        last_reader_activity_monotonic + _OPEN_ENDED_SSE_FOLLOW_UP_QUIET_WINDOW_S
                    )
                    if not reader_event_in_progress and quiet_deadline <= now:
                        return parsed_response
                    if reader_event_in_progress:
                        wait_timeout = response_deadline - now
                    else:
                        wait_timeout = min(quiet_deadline - now, response_deadline - now)
                try:
                    item = reader_items.get(timeout=max(wait_timeout, 0.0))
                except queue.Empty:
                    snapshot = reader_state.snapshot()
                    if snapshot.activity_sequence > last_observed_activity_sequence:
                        last_observed_activity_sequence = snapshot.activity_sequence
                        last_reader_activity_monotonic = snapshot.activity_monotonic
                    reader_event_in_progress = snapshot.event_in_progress
                    continue
                last_observed_activity_sequence = max(
                    last_observed_activity_sequence,
                    item.activity_sequence,
                )
                if item.kind == "closed":
                    if parsed_response is None:
                        raise self._build_error(
                            "HTTP event stream ended before the matching JSON-RPC response "
                            f"arrived for '{method}'",
                            exc_type=McpHttpTransportProtocolError,
                        )
                    last_reader_activity_monotonic = max(
                        last_reader_activity_monotonic or item.activity_monotonic,
                        item.activity_monotonic,
                    )
                    return parsed_response
                if item.kind == "error":
                    error = item.error or RuntimeError("unknown HTTP event-stream reader failure")
                    self._raise_sse_reader_error(
                        method=method,
                        timeout_s=timeout_s,
                        error=error,
                    )
                event = item.event
                if event is None or not event.data.strip():
                    continue
                last_reader_activity_monotonic = item.activity_monotonic
                message = self._parse_single_jsonrpc_payload(event.data, method=method)
                if parsed_response is None:
                    parsed_message, response_received = self._process_jsonrpc_message(
                        message,
                        method=method,
                        expected_response_id=expected_response_id,
                        response_received=False,
                        server_request_timeout_s=max(response_deadline - time.monotonic(), 0.001),
                        allow_server_requests=True,
                    )
                    if parsed_message is None or not response_received:
                        continue
                    parsed_response = parsed_message
                    continue
                self._handle_open_ended_follow_up_message(message, method=method)
        finally:
            response.close()
            reader_thread.join(timeout=_OPEN_ENDED_SSE_READER_JOIN_TIMEOUT_S)

    def _handle_event_stream(
        self,
        response: httpx.Response,
        *,
        method: str,
        expected_response_id: int | str | None,
        timeout_s: float,
    ) -> JsonRpcResponse | None:
        has_content_length = response.headers.get("content-length") is not None
        if not has_content_length and expected_response_id is not None:
            return self._handle_open_ended_request_event_stream(
                response,
                method=method,
                expected_response_id=expected_response_id,
                timeout_s=timeout_s,
            )
        response_received = False
        parsed_response: JsonRpcResponse | None = None
        for event in _iter_sse_events(response):
            if not event.data.strip():
                continue
            message = self._parse_single_jsonrpc_payload(event.data, method=method)
            parsed_message, response_received = self._process_jsonrpc_message(
                message,
                method=method,
                expected_response_id=expected_response_id,
                response_received=response_received,
                server_request_timeout_s=timeout_s,
                allow_server_requests=expected_response_id is not None and not response_received,
            )
            if parsed_message is not None:
                parsed_response = parsed_message
        if expected_response_id is None:
            return None
        if parsed_response is None:
            raise self._build_error(
                f"HTTP event stream ended before the matching JSON-RPC response arrived for "
                f"'{method}'",
                exc_type=McpHttpTransportProtocolError,
            )
        return parsed_response

    def _send_jsonrpc_message(
        self,
        *,
        payload: dict[str, Any],
        method: str,
        timeout_s: float,
        expected_response_id: int | str | None,
    ) -> JsonRpcResponse | None:
        if self._closed:
            raise self._build_error("transport is already closed.")
        request_body = encode_jsonrpc_message(payload)
        had_session_id = bool(self._session_id)
        allow_request_replay = method != "tools/call"
        oauth_retry_attempted = False
        try:
            while True:
                oauth_record: McpOAuthTokenRecord | None = None
                access_token: str | None = None
                if self._oauth_enabled():
                    oauth_record = self._resolve_oauth_token_record(method=method)
                    access_token = oauth_record.access_token
                request_headers = self._build_headers(access_token=access_token)
                with self._client.stream(
                    "POST",
                    self.server.url,
                    headers=request_headers,
                    content=request_body.encode("utf-8"),
                    timeout=timeout_s,
                ) as response:
                    if self._oauth_enabled() and self._handle_oauth_http_status(
                        response,
                        method=method,
                        oauth_record=oauth_record,
                        allow_request_replay=allow_request_replay,
                        oauth_retry_attempted=oauth_retry_attempted,
                    ):
                        oauth_retry_attempted = True
                        continue
                    self._raise_for_http_status(
                        response,
                        method=method,
                        had_session_id=had_session_id,
                    )
                    self._capture_session_headers(response, method=method)
                    content_type = _normalized_content_type(response.headers.get("content-type"))
                    if not content_type:
                        if expected_response_id is None and response.status_code in {202, 204}:
                            return None
                        if expected_response_id is None and not response.read():
                            return None
                        raise self._build_error(
                            f"HTTP MCP '{method}' returned no content type",
                            exc_type=McpHttpTransportProtocolError,
                        )
                    if content_type == "application/json":
                        return self._handle_application_json(
                            response,
                            method=method,
                            expected_response_id=expected_response_id,
                        )
                    if content_type == "text/event-stream":
                        return self._handle_event_stream(
                            response,
                            method=method,
                            expected_response_id=expected_response_id,
                            timeout_s=timeout_s,
                        )
                    raise self._build_error(
                        f"HTTP MCP '{method}' returned unsupported content type {content_type!r}",
                        exc_type=McpHttpTransportProtocolError,
                    )
        except (McpHttpTransportError, McpOAuthError):
            raise
        except httpx.TimeoutException as exc:
            raise self._build_error(
                f"HTTP MCP '{method}' timed out after {timeout_s:.3f}s",
                exc_type=McpHttpTransportTimeoutError,
            ) from exc
        except httpx.HTTPError as exc:
            raise self._build_error(
                f"HTTP MCP '{method}' request failed: {exc}",
                exc_type=McpHttpTransportError,
            ) from exc

    def request(
        self,
        *,
        method: str,
        params: Any | None,
        timeout_s: float,
    ) -> JsonRpcResponse:
        request_id = self._id_generator.next()
        response = self._send_jsonrpc_message(
            payload=build_jsonrpc_request(
                request_id=request_id,
                method=method,
                params=params,
            ),
            method=method,
            timeout_s=timeout_s,
            expected_response_id=request_id,
        )
        if response is None:
            raise self._build_error(
                f"HTTP MCP '{method}' completed without the matching JSON-RPC response",
                exc_type=McpHttpTransportProtocolError,
            )
        return response

    def send_notification(
        self,
        *,
        method: str,
        params: Any | None,
        completion_timeout_s: float | None = None,
    ) -> None:
        timeout_s = completion_timeout_s or self.server.call_timeout_s
        self._send_jsonrpc_message(
            payload=build_jsonrpc_notification(
                method=method,
                params=params,
            ),
            method=method,
            timeout_s=timeout_s,
            expected_response_id=None,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session_id:
            try:
                delete_headers = dict(self.server.headers)
                delete_headers["User-Agent"] = _USER_AGENT
                if self._oauth_enabled():
                    try:
                        close_record = load_oauth_token_record(self.server.id)
                    except McpOAuthTokenStoreError:
                        close_record = None
                    if close_record is not None:
                        delete_headers["Authorization"] = f"Bearer {close_record.access_token}"
                delete_headers["MCP-Session-Id"] = self._session_id
                if self._negotiated_protocol_version:
                    delete_headers["MCP-Protocol-Version"] = self._negotiated_protocol_version
                response = self._client.delete(
                    self.server.url,
                    headers=delete_headers,
                    timeout=min(self.server.call_timeout_s, 2.0),
                )
                if response.status_code not in {200, 202, 204, 404, 405}:
                    response.read()
            except Exception:
                pass
        if self._owns_client:
            self._client.close()
