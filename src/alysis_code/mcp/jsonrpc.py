from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeAlias

from .errors import McpProtocolError

JsonRpcId: TypeAlias = int | str


class JsonRpcProtocolError(McpProtocolError):
    pass


def _validate_request_id(value: object, *, field_name: str = "id") -> JsonRpcId:
    if isinstance(value, bool):
        raise JsonRpcProtocolError(f"JSON-RPC {field_name} must be a string or integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise JsonRpcProtocolError(f"JSON-RPC {field_name} cannot be empty.")
        return cleaned
    raise JsonRpcProtocolError(f"JSON-RPC {field_name} must be a string or integer.")


def _validate_method(value: object) -> str:
    if not isinstance(value, str):
        raise JsonRpcProtocolError("JSON-RPC method must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise JsonRpcProtocolError("JSON-RPC method cannot be empty.")
    return cleaned


def _validate_envelope_version(payload: dict[str, Any]) -> None:
    if payload.get("jsonrpc") != "2.0":
        raise JsonRpcProtocolError("JSON-RPC envelope must include jsonrpc='2.0'.")


@dataclass(frozen=True)
class JsonRpcErrorObject:
    code: int
    message: str
    data: Any | None = None

    @classmethod
    def from_payload(cls, payload: object) -> JsonRpcErrorObject:
        if not isinstance(payload, dict):
            raise JsonRpcProtocolError("JSON-RPC error payload must be an object.")
        code = payload.get("code")
        message = payload.get("message")
        if isinstance(code, bool) or not isinstance(code, int):
            raise JsonRpcProtocolError("JSON-RPC error.code must be an integer.")
        if not isinstance(message, str) or not message.strip():
            raise JsonRpcProtocolError("JSON-RPC error.message must be a non-empty string.")
        return cls(code=code, message=message.strip(), data=payload.get("data"))

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "message": self.message,
        }
        if self.data is not None:
            payload["data"] = self.data
        return payload


@dataclass(frozen=True)
class JsonRpcRequest:
    id: JsonRpcId
    method: str
    params: Any | None = None


@dataclass(frozen=True)
class JsonRpcNotification:
    method: str
    params: Any | None = None


@dataclass(frozen=True)
class JsonRpcResponse:
    id: JsonRpcId
    result: Any | None = None
    error: JsonRpcErrorObject | None = None


JsonRpcMessage: TypeAlias = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse


class JsonRpcIdGenerator:
    def __init__(self, *, start: int = 1) -> None:
        self._next_id = int(start)

    def next(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id


def encode_jsonrpc_message(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def build_jsonrpc_request(
    *,
    request_id: JsonRpcId,
    method: str,
    params: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": _validate_request_id(request_id),
        "method": _validate_method(method),
    }
    if params is not None:
        payload["params"] = params
    return payload


def build_jsonrpc_notification(
    *,
    method: str,
    params: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": _validate_method(method),
    }
    if params is not None:
        payload["params"] = params
    return payload


def build_jsonrpc_result_response(
    *,
    request_id: JsonRpcId,
    result: Any,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": _validate_request_id(request_id),
        "result": result,
    }


def build_jsonrpc_error_response(
    *,
    request_id: JsonRpcId,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error = JsonRpcErrorObject(code=code, message=str(message).strip(), data=data)
    return {
        "jsonrpc": "2.0",
        "id": _validate_request_id(request_id),
        "error": error.as_payload(),
    }


def _parse_jsonrpc_message(payload: object) -> JsonRpcMessage:
    if not isinstance(payload, dict):
        raise JsonRpcProtocolError("JSON-RPC message must be an object.")
    _validate_envelope_version(payload)

    has_method = "method" in payload
    has_id = "id" in payload
    has_result = "result" in payload
    has_error = "error" in payload

    if has_method:
        method = _validate_method(payload.get("method"))
        if has_result or has_error:
            raise JsonRpcProtocolError(
                "JSON-RPC request/notification cannot include result or error fields."
            )
        params = payload.get("params")
        if has_id:
            return JsonRpcRequest(
                id=_validate_request_id(payload.get("id")),
                method=method,
                params=params,
            )
        return JsonRpcNotification(method=method, params=params)

    if not has_id:
        raise JsonRpcProtocolError("JSON-RPC response must include an id field.")
    if has_result == has_error:
        raise JsonRpcProtocolError("JSON-RPC response must include exactly one of result or error.")
    request_id = _validate_request_id(payload.get("id"))
    if has_error:
        return JsonRpcResponse(
            id=request_id, error=JsonRpcErrorObject.from_payload(payload["error"])
        )
    return JsonRpcResponse(id=request_id, result=payload.get("result"))


def parse_jsonrpc_line(raw_line: str) -> tuple[JsonRpcMessage, ...]:
    text = str(raw_line).strip()
    if not text:
        raise JsonRpcProtocolError("JSON-RPC line cannot be empty.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonRpcProtocolError(f"Malformed JSON-RPC JSON: {exc.msg}") from exc
    if isinstance(payload, list):
        if not payload:
            raise JsonRpcProtocolError("JSON-RPC batch payload cannot be empty.")
        return tuple(_parse_jsonrpc_message(item) for item in payload)
    return (_parse_jsonrpc_message(payload),)
