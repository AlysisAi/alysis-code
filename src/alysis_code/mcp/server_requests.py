from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..runtime_kind import RuntimeKind, normalize_runtime_kind
from .errors import McpNotSupportedError, McpProtocolError
from .jsonrpc import JsonRpcId

_JSONRPC_METHOD_NOT_FOUND_CODE = -32601
_JSONRPC_INVALID_PARAMS_CODE = -32602


@dataclass(frozen=True)
class McpServerRequestContext:
    server_id: str
    workspace_root: Path
    runtime_kind: RuntimeKind

    @classmethod
    def create(
        cls,
        *,
        server_id: str,
        workspace_root: Path,
        runtime_kind: RuntimeKind | str | None,
        fallback_runtime_kind: RuntimeKind,
    ) -> McpServerRequestContext:
        return cls(
            server_id=str(server_id).strip(),
            workspace_root=workspace_root.resolve(),
            runtime_kind=normalize_runtime_kind(runtime_kind, fallback=fallback_runtime_kind),
        )


class McpServerRequestHandlerError(McpProtocolError):
    def __init__(self, *, code: int, message: str) -> None:
        cleaned = str(message or "").strip() or "Server request handling failed."
        super().__init__(cleaned)
        self.code = int(code)
        self.message = cleaned


class McpUnsupportedServerRequestError(McpServerRequestHandlerError, McpNotSupportedError):
    def __init__(self, *, method: str) -> None:
        super().__init__(
            code=_JSONRPC_METHOD_NOT_FOUND_CODE,
            message=f"Server-initiated request '{method}' is not supported by this MCP host runtime.",
        )


class McpInvalidServerRequestParamsError(McpServerRequestHandlerError):
    def __init__(self, *, method: str, detail: str | None = None) -> None:
        cleaned_detail = str(detail or "").strip()
        message = f"Invalid params for server-initiated request '{method}'."
        if cleaned_detail:
            message += f" {cleaned_detail}"
        super().__init__(
            code=_JSONRPC_INVALID_PARAMS_CODE,
            message=message,
        )


class McpServerRequestHandler(Protocol):
    def client_capabilities(self, *, context: McpServerRequestContext) -> dict[str, Any]: ...

    def handle_request(
        self,
        *,
        context: McpServerRequestContext,
        method: str,
        request_id: JsonRpcId,
        params: Any | None,
    ) -> dict[str, Any]: ...
