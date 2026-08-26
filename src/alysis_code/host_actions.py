from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection
from typing import Any, TypeAlias

HOST_ACTION_PROTOCOL_VERSION = "1"
HOST_ACTIONS: tuple[str, ...] = (
    "tasks.list",
    "tasks.run",
    "tasks.status",
    "tasks.terminate",
    "debug.list",
    "debug.start",
    "debug.stop",
    "debug.status",
)
HOST_ACTION_SET = frozenset(HOST_ACTIONS)
HOST_ACTION_TOOL_NAMES: dict[str, str] = {
    "tasks.list": "ide_task_list",
    "tasks.run": "ide_task_run",
    "tasks.status": "ide_task_status",
    "tasks.terminate": "ide_task_terminate",
    "debug.list": "ide_debug_list",
    "debug.start": "ide_debug_start",
    "debug.stop": "ide_debug_stop",
    "debug.status": "ide_debug_status",
}
HOST_ACTION_MAX_ARGUMENT_BYTES = 8 * 1024
HOST_ACTION_MAX_RESULT_BYTES = 64 * 1024
HOST_ACTION_MAX_LIST_ITEMS = 100
HOST_ACTION_MAX_IDENTIFIER_CHARS = 256
HOST_ACTION_MAX_LABEL_CHARS = 512
HOST_ACTION_MAX_ERROR_MESSAGE_CHARS = 512
HOST_ACTION_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class HostActionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        host_error_code: str | None = None,
    ) -> None:
        self.code = str(code or "host_action_failed")
        self.message = str(message or "The IDE host action failed.")
        self.retryable = bool(retryable)
        self.host_error_code = str(host_error_code or "") or None
        super().__init__(self.message)

    def to_result_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.message,
            "error_code": self.code,
            "retryable": self.retryable,
        }
        if self.host_error_code:
            payload["host_error_code"] = self.host_error_code
        return payload


HostActionHandler: TypeAlias = Callable[[str, dict[str, Any]], dict[str, Any]]


def normalized_host_action_capabilities(actions: Collection[str] | None) -> frozenset[str]:
    if actions is None:
        return frozenset()
    return frozenset(
        action.strip()
        for action in actions
        if isinstance(action, str) and action.strip() in HOST_ACTION_SET
    )


def normalize_host_action_arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    normalized_action = str(action or "").strip()
    if normalized_action not in HOST_ACTION_SET:
        raise HostActionError(
            "unsupported_host_action",
            "The requested IDE host action is not supported.",
        )
    if not isinstance(arguments, dict):
        raise HostActionError(
            "invalid_host_action_arguments",
            "IDE host action arguments must be an object.",
        )

    if normalized_action == "tasks.list":
        _reject_extra_keys(arguments, {"task_type"})
        task_type = _optional_identifier(arguments.get("task_type"), "task_type")
        normalized = {"task_type": task_type} if task_type else {}
    elif normalized_action == "tasks.run":
        _reject_extra_keys(arguments, {"task_id"})
        normalized = {"task_id": _required_identifier(arguments.get("task_id"), "task_id")}
    elif normalized_action == "tasks.status":
        _reject_extra_keys(arguments, {"execution_id"})
        execution_id = _optional_identifier(arguments.get("execution_id"), "execution_id")
        normalized = {"execution_id": execution_id} if execution_id else {}
    elif normalized_action == "tasks.terminate":
        _reject_extra_keys(arguments, {"execution_id"})
        normalized = {
            "execution_id": _required_identifier(arguments.get("execution_id"), "execution_id")
        }
    elif normalized_action == "debug.list":
        _reject_extra_keys(arguments, set())
        normalized = {}
    elif normalized_action == "debug.start":
        _reject_extra_keys(arguments, {"configuration_id"})
        normalized = {
            "configuration_id": _required_identifier(
                arguments.get("configuration_id"), "configuration_id"
            )
        }
    elif normalized_action == "debug.stop":
        _reject_extra_keys(arguments, {"debug_session_id"})
        normalized = {
            "debug_session_id": _required_identifier(
                arguments.get("debug_session_id"), "debug_session_id"
            )
        }
    else:
        _reject_extra_keys(arguments, {"debug_session_id"})
        debug_session_id = _optional_identifier(
            arguments.get("debug_session_id"), "debug_session_id"
        )
        normalized = {"debug_session_id": debug_session_id} if debug_session_id else {}

    if json_size_bytes(normalized) > HOST_ACTION_MAX_ARGUMENT_BYTES:
        raise HostActionError(
            "host_action_arguments_too_large",
            "IDE host action arguments exceed the allowed size.",
        )
    return normalized


def normalize_host_action_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    normalized_action = str(action or "").strip()
    if normalized_action not in HOST_ACTION_SET:
        raise HostActionError(
            "unsupported_host_action",
            "The requested IDE host action is not supported.",
        )
    if not isinstance(result, dict):
        raise HostActionError(
            "invalid_host_action_result",
            "IDE host action result must be an object.",
        )
    if json_size_bytes(result) > HOST_ACTION_MAX_RESULT_BYTES:
        raise HostActionError(
            "host_action_result_too_large",
            "IDE host action result exceeds the allowed size.",
        )

    if normalized_action == "tasks.list":
        normalized = _normalize_task_list_result(result)
    elif normalized_action == "tasks.run":
        _reject_extra_keys(
            result,
            {"execution_id", "task_id", "state", "exit_code", "diagnostics_delta"},
        )
        normalized = {
            "execution_id": _required_identifier(result.get("execution_id"), "execution_id"),
            "task_id": _required_identifier(result.get("task_id"), "task_id"),
            "state": _enum_value(result.get("state"), "state", {"started", "completed", "failed"}),
        }
        if result.get("exit_code") is not None:
            normalized["exit_code"] = _bounded_int(result.get("exit_code"), "exit_code")
        if result.get("diagnostics_delta") is not None:
            normalized["diagnostics_delta"] = _normalize_diagnostics_delta(
                result.get("diagnostics_delta")
            )
    elif normalized_action == "tasks.status":
        normalized = _normalize_task_status_result(result)
    elif normalized_action == "tasks.terminate":
        _reject_extra_keys(result, {"execution_id", "terminated", "state"})
        normalized = {
            "execution_id": _required_identifier(result.get("execution_id"), "execution_id"),
            "terminated": _required_bool(result.get("terminated"), "terminated"),
            "state": _enum_value(
                result.get("state"), "state", {"terminated", "not_found", "already_ended"}
            ),
        }
    elif normalized_action == "debug.list":
        normalized = _normalize_debug_list_result(result)
    elif normalized_action == "debug.start":
        _reject_extra_keys(result, {"debug_session_id", "configuration_id", "state"})
        normalized = {
            "debug_session_id": _required_identifier(
                result.get("debug_session_id"), "debug_session_id"
            ),
            "configuration_id": _required_identifier(
                result.get("configuration_id"), "configuration_id"
            ),
            "state": _enum_value(result.get("state"), "state", {"started", "failed"}),
        }
    elif normalized_action == "debug.stop":
        _reject_extra_keys(result, {"debug_session_id", "stopped", "state"})
        normalized = {
            "debug_session_id": _required_identifier(
                result.get("debug_session_id"), "debug_session_id"
            ),
            "stopped": _required_bool(result.get("stopped"), "stopped"),
            "state": _enum_value(
                result.get("state"), "state", {"stopped", "not_found", "already_ended"}
            ),
        }
    else:
        normalized = _normalize_debug_status_result(result)

    if json_size_bytes(normalized) > HOST_ACTION_MAX_RESULT_BYTES:
        raise HostActionError(
            "host_action_result_too_large",
            "IDE host action result exceeds the allowed size.",
        )
    return normalized


def json_size_bytes(value: Any) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HostActionError(
            "invalid_host_action_payload",
            "IDE host action payload must contain finite JSON values.",
        ) from exc
    return len(encoded)


def normalize_host_error(error: dict[str, Any]) -> tuple[str, str, bool]:
    if not isinstance(error, dict):
        raise HostActionError(
            "invalid_host_action_error",
            "IDE host action error must be an object.",
        )
    _reject_extra_keys(error, {"code", "message", "retryable"})
    raw_code = error.get("code")
    code = raw_code.strip() if isinstance(raw_code, str) else ""
    if not HOST_ACTION_ERROR_CODE_PATTERN.fullmatch(code):
        raise HostActionError(
            "invalid_host_action_error",
            "IDE host action error code is invalid.",
        )
    raw_message = error.get("message")
    message = raw_message.strip() if isinstance(raw_message, str) else ""
    if (
        not message
        or len(message) > HOST_ACTION_MAX_ERROR_MESSAGE_CHARS
        or _has_disallowed_text_control(message)
    ):
        raise HostActionError(
            "invalid_host_action_error",
            "IDE host action error message is invalid.",
        )
    retryable = error.get("retryable", False)
    if not isinstance(retryable, bool):
        raise HostActionError(
            "invalid_host_action_error",
            "IDE host action retryable must be a boolean.",
        )
    return code, message, retryable


def _normalize_task_list_result(result: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(result, {"tasks", "truncated"})
    items = result.get("tasks")
    if not isinstance(items, list):
        raise HostActionError("invalid_host_action_result", "tasks must be an array.")
    normalized_items: list[dict[str, Any]] = []
    for raw in items[:HOST_ACTION_MAX_LIST_ITEMS]:
        if not isinstance(raw, dict):
            raise HostActionError("invalid_host_action_result", "Each task must be an object.")
        _reject_extra_keys(raw, {"id", "label", "type", "source", "detail"})
        item: dict[str, Any] = {
            "id": _required_identifier(raw.get("id"), "task.id"),
            "label": _required_label(raw.get("label"), "task.label"),
        }
        for key in ("type", "source", "detail"):
            value = _optional_label(raw.get(key), f"task.{key}")
            if value:
                item[key] = value
        normalized_items.append(item)
    return {
        "tasks": normalized_items,
        "truncated": bool(
            _optional_bool(result.get("truncated"), "truncated")
            or len(items) > len(normalized_items)
        ),
    }


def _normalize_task_status_result(result: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(result, {"executions", "truncated"})
    items = result.get("executions")
    if not isinstance(items, list):
        raise HostActionError("invalid_host_action_result", "executions must be an array.")
    normalized_items: list[dict[str, Any]] = []
    for raw in items[:HOST_ACTION_MAX_LIST_ITEMS]:
        if not isinstance(raw, dict):
            raise HostActionError(
                "invalid_host_action_result", "Each task execution must be an object."
            )
        _reject_extra_keys(
            raw,
            {"execution_id", "task_id", "state", "exit_code", "diagnostics_delta"},
        )
        item: dict[str, Any] = {
            "execution_id": _required_identifier(raw.get("execution_id"), "execution_id"),
            "task_id": _required_identifier(raw.get("task_id"), "task_id"),
            "state": _enum_value(
                raw.get("state"),
                "task_execution.state",
                {"running", "completed", "failed", "terminated"},
            ),
        }
        if raw.get("exit_code") is not None:
            item["exit_code"] = _bounded_int(raw.get("exit_code"), "exit_code")
        if raw.get("diagnostics_delta") is not None:
            item["diagnostics_delta"] = _normalize_diagnostics_delta(raw.get("diagnostics_delta"))
        normalized_items.append(item)
    return {
        "executions": normalized_items,
        "truncated": bool(
            _optional_bool(result.get("truncated"), "truncated")
            or len(items) > len(normalized_items)
        ),
    }


def _normalize_debug_list_result(result: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(result, {"configurations", "truncated"})
    items = result.get("configurations")
    if not isinstance(items, list):
        raise HostActionError("invalid_host_action_result", "configurations must be an array.")
    normalized_items: list[dict[str, Any]] = []
    for raw in items[:HOST_ACTION_MAX_LIST_ITEMS]:
        if not isinstance(raw, dict):
            raise HostActionError(
                "invalid_host_action_result", "Each debug configuration must be an object."
            )
        _reject_extra_keys(raw, {"id", "name", "type", "request", "workspace_folder"})
        item: dict[str, Any] = {
            "id": _required_identifier(raw.get("id"), "configuration.id"),
            "name": _required_label(raw.get("name"), "configuration.name"),
            "type": _required_label(raw.get("type"), "configuration.type"),
        }
        request = _optional_label(raw.get("request"), "configuration.request")
        if request:
            item["request"] = request
        workspace_folder = _optional_label(
            raw.get("workspace_folder"), "configuration.workspace_folder"
        )
        if workspace_folder:
            item["workspace_folder"] = workspace_folder
        normalized_items.append(item)
    return {
        "configurations": normalized_items,
        "truncated": bool(
            _optional_bool(result.get("truncated"), "truncated")
            or len(items) > len(normalized_items)
        ),
    }


def _normalize_debug_status_result(result: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(result, {"sessions", "truncated"})
    items = result.get("sessions")
    if not isinstance(items, list):
        raise HostActionError("invalid_host_action_result", "sessions must be an array.")
    normalized_items: list[dict[str, Any]] = []
    for raw in items[:HOST_ACTION_MAX_LIST_ITEMS]:
        if not isinstance(raw, dict):
            raise HostActionError(
                "invalid_host_action_result", "Each debug session must be an object."
            )
        _reject_extra_keys(raw, {"id", "name", "type", "state", "workspace_folder"})
        item: dict[str, Any] = {
            "id": _required_identifier(raw.get("id"), "debug_session.id"),
            "name": _required_label(raw.get("name"), "debug_session.name"),
            "type": _required_label(raw.get("type"), "debug_session.type"),
            "state": _enum_value(raw.get("state"), "debug_session.state", {"running", "stopped"}),
        }
        workspace_folder = _optional_label(
            raw.get("workspace_folder"), "debug_session.workspace_folder"
        )
        if workspace_folder:
            item["workspace_folder"] = workspace_folder
        normalized_items.append(item)
    return {
        "sessions": normalized_items,
        "truncated": bool(
            _optional_bool(result.get("truncated"), "truncated")
            or len(items) > len(normalized_items)
        ),
    }


def _normalize_diagnostics_delta(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HostActionError("invalid_host_action_result", "diagnostics_delta must be an object.")
    _reject_extra_keys(value, {"added", "removed", "changed", "total", "truncated", "items"})
    items = value.get("items", [])
    if not isinstance(items, list):
        raise HostActionError(
            "invalid_host_action_result", "diagnostics_delta.items must be an array."
        )
    normalized_items: list[dict[str, Any]] = []
    for raw in items[:50]:
        if not isinstance(raw, dict):
            raise HostActionError(
                "invalid_host_action_result", "Each diagnostic delta item must be an object."
            )
        _reject_extra_keys(raw, {"uri", "line", "severity", "message", "source"})
        raw_uri = raw.get("uri")
        uri = raw_uri.strip() if isinstance(raw_uri, str) else ""
        if not uri or len(uri) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in uri):
            raise HostActionError(
                "invalid_host_action_result", "diagnostic uri must be bounded non-empty text."
            )
        line = raw.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line < 2**31:
            raise HostActionError(
                "invalid_host_action_result", "diagnostic line must be a positive integer."
            )
        item: dict[str, Any] = {
            "uri": uri,
            "line": line,
            "severity": _enum_value(
                raw.get("severity"),
                "diagnostic.severity",
                {"error", "warning", "information", "hint"},
            ),
            "message": _required_label(raw.get("message"), "diagnostic.message"),
        }
        source = _optional_label(raw.get("source"), "diagnostic.source")
        if source:
            item["source"] = source
        normalized_items.append(item)
    return {
        "added": _nonnegative_int(value.get("added"), "diagnostics_delta.added"),
        "removed": _nonnegative_int(value.get("removed"), "diagnostics_delta.removed"),
        "changed": _nonnegative_int(value.get("changed"), "diagnostics_delta.changed"),
        "total": _nonnegative_int(value.get("total"), "diagnostics_delta.total"),
        "truncated": bool(
            _optional_bool(value.get("truncated"), "diagnostics_delta.truncated")
            or len(items) > len(normalized_items)
        ),
        "items": normalized_items,
    }


def _reject_extra_keys(value: dict[str, Any], allowed: set[str]) -> None:
    if extra := sorted(set(value) - allowed):
        preview = ", ".join(str(key)[:64] for key in extra[:5])
        if len(extra) > 5:
            preview += ", ..."
        raise HostActionError(
            "invalid_host_action_payload",
            "IDE host action payload contains unsupported field(s): " + preview,
        )


def _required_identifier(value: Any, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > HOST_ACTION_MAX_IDENTIFIER_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise HostActionError(
            "invalid_host_action_payload", f"{field} must be a bounded non-empty identifier."
        )
    return text


def _optional_identifier(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, field)


def _required_label(value: Any, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > HOST_ACTION_MAX_LABEL_CHARS or _has_disallowed_text_control(text):
        raise HostActionError(
            "invalid_host_action_payload", f"{field} must be bounded non-empty text."
        )
    return text


def _optional_label(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_label(value, field)


def _enum_value(value: Any, field: str, allowed: set[str]) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if text not in allowed:
        raise HostActionError(
            "invalid_host_action_payload",
            f"{field} must be one of: " + ", ".join(sorted(allowed)),
        )
    return text


def _has_disallowed_text_control(value: str) -> bool:
    return any(
        (ord(char) < 32 and char not in {"\t", "\n", "\r"}) or ord(char) == 127 for char in value
    )


def _required_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise HostActionError("invalid_host_action_payload", f"{field} must be a boolean.")
    return value


def _optional_bool(value: Any, field: str) -> bool:
    if value is None:
        return False
    return _required_bool(value, field)


def _bounded_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**31) <= value < 2**31:
        raise HostActionError("invalid_host_action_payload", f"{field} must be a 32-bit integer.")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    normalized = _bounded_int(value, field)
    if normalized < 0:
        raise HostActionError("invalid_host_action_payload", f"{field} must be non-negative.")
    return normalized
