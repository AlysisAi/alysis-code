from __future__ import annotations

import copy
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any

from ..tools.fs import classify_sensitive_path

_SENSITIVE_POLICY_KEY = "_alysis_output_policy"
_REDACTED_ARGUMENT = "[redacted: sensitive file content]"
_REDACTED_MESSAGE = "Sensitive tool output redacted."
_REDACTED_RESPONSE_TAINT = "[redacted: approved sensitive content]"
_TERMINAL_SENSITIVE_READ_ERROR = (
    "Sensitive path is protected and will not be readable after this failure. "
    "No content was returned. This failure is terminal; do not retry."
)
_CONTENT_ARGUMENT_KEYS = frozenset(
    {
        "body",
        "bytes",
        "content",
        "contents",
        "data",
        "diff",
        "edit",
        "edits",
        "expected",
        "expected_old",
        "insert",
        "lines",
        "new",
        "new_content",
        "old",
        "old_content",
        "patch",
        "replacement",
        "text",
        "value",
    }
)
_DIRECT_PATH_FIELDS = (
    "path",
    "source_path",
    "destination_path",
    "file",
    "file_path",
)
_PATCH_PATH_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:[ab]/)?([^\t\r\n]+)", re.MULTILINE)
_TAINT_VALUE_KEYS = _CONTENT_ARGUMENT_KEYS | frozenset({"insert_content", "replacement", "target"})


@dataclass(frozen=True, slots=True)
class SensitiveToolBoundary:
    """Persistence/display policy derived without opening sensitive files."""

    sensitive: bool = False
    paths: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PersistenceSafeToolCall:
    id: str
    name: str
    arguments: Any
    provider_metadata: Any = None


@dataclass(frozen=True, slots=True)
class _PersistenceSafeResponse:
    content: str
    tool_calls: list[Any]
    raw: dict[str, Any]
    response_model: Any = None
    usage: Any = None
    provider_metadata: Any = None
    reasoning: Any = ()
    assistant_phase: Any = None


def _normalized_unique_strings(values: list[Any]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip().replace("\\", "/")
        if not item or item == "/dev/null":
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return tuple(normalized)


def _candidate_paths(tool_name: str, arguments: Any) -> tuple[str, ...]:
    if not isinstance(arguments, dict):
        return ()
    normalized_name = str(tool_name or "").strip().casefold()
    if not (normalized_name.startswith("fs_") or normalized_name == "git_apply_patch"):
        return ()
    candidates = [arguments.get(field) for field in _DIRECT_PATH_FIELDS]
    if normalized_name == "git_apply_patch":
        patch = arguments.get("patch")
        if patch is None:
            patch = arguments.get("diff")
        if patch is not None:
            candidates.extend(_PATCH_PATH_RE.findall(str(patch)))
    return _normalized_unique_strings(candidates)


def _policy_categories(result: Any) -> tuple[str, ...]:
    if not isinstance(result, dict):
        return ()
    policy = result.get(_SENSITIVE_POLICY_KEY)
    if not isinstance(policy, dict) or policy.get("sensitive") is not True:
        return ()
    if str(policy.get("persist") or "").casefold() != "redact":
        return ()
    if str(policy.get("display") or "").casefold() != "redact":
        return ()
    raw_categories = policy.get("categories")
    if not isinstance(raw_categories, list | tuple):
        return ()
    return _normalized_unique_strings(list(raw_categories))


def sensitive_tool_boundary(
    tool_name: str,
    arguments: Any,
    *,
    result: Any = None,
) -> SensitiveToolBoundary:
    paths = _candidate_paths(tool_name, arguments)
    categories: list[str] = []
    sensitive_paths: list[str] = []
    for path in paths:
        classification = classify_sensitive_path(path)
        if not classification.sensitive:
            continue
        sensitive_paths.append(path)
        categories.append(str(classification.category or "sensitive_file"))

    policy_categories = _policy_categories(result)
    policy = result.get(_SENSITIVE_POLICY_KEY) if isinstance(result, dict) else None
    policy_sensitive = isinstance(policy, dict) and policy.get("sensitive") is True
    if not sensitive_paths and not policy_sensitive:
        return SensitiveToolBoundary()
    return SensitiveToolBoundary(
        sensitive=True,
        paths=_normalized_unique_strings(sensitive_paths or list(paths)),
        categories=_normalized_unique_strings(categories + list(policy_categories)),
    )


def _redact_content_fields(value: Any, *, parent_key: str = "") -> Any:
    if parent_key.casefold() in _CONTENT_ARGUMENT_KEYS:
        return _REDACTED_ARGUMENT
    if isinstance(value, dict):
        return {
            str(key): _redact_content_fields(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_content_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_content_fields(item) for item in value)
    return copy.deepcopy(value)


def redact_sensitive_tool_arguments(
    tool_name: str,
    arguments: Any,
    *,
    boundary: SensitiveToolBoundary | None = None,
) -> Any:
    effective = boundary or sensitive_tool_boundary(tool_name, arguments)
    if not effective.sensitive:
        return arguments
    return _redact_content_fields(arguments)


def sensitive_result_stub(
    tool_name: str,
    boundary: SensitiveToolBoundary,
) -> dict[str, Any]:
    """Return the only representation allowed beyond the immediate model call."""

    return {
        "_alysis_redacted": True,
        "sensitive": True,
        "persist": "redact",
        "display": "redact",
        "tool": str(tool_name or ""),
        "paths": list(boundary.paths),
        "categories": list(boundary.categories),
        "message": _REDACTED_MESSAGE,
    }


def redact_sensitive_tool_result(
    tool_name: str,
    arguments: Any,
    result: Any,
    *,
    boundary: SensitiveToolBoundary | None = None,
) -> Any:
    effective = boundary or sensitive_tool_boundary(tool_name, arguments, result=result)
    if not effective.sensitive:
        return result
    stub = sensitive_result_stub(tool_name, effective)
    if isinstance(result, dict) and "error" in result:
        normalized_tool = str(tool_name or "").strip().casefold()
        if normalized_tool in {"fs_read", "fs_read_lines"}:
            if result.get("error_code") == "fs_path_not_found":
                path = effective.paths[0] if effective.paths else "requested path"
                stub["error"] = (
                    f"Path does not exist: {path}. This result is terminal; do not retry this path."
                )
                stub["error_code"] = "fs_path_not_found"
            else:
                stub["error"] = _TERMINAL_SENSITIVE_READ_ERROR
                stub["error_code"] = "sensitive_read_terminal"
            stub["terminal"] = True
            stub["retryable"] = False
        else:
            stub["error"] = "Sensitive tool operation failed; details redacted."
    return stub


def _add_taint_variants(values: set[str], raw_value: Any) -> None:
    if isinstance(raw_value, bytes):
        text = raw_value.decode("utf-8", errors="replace")
    elif isinstance(raw_value, str):
        text = raw_value
    else:
        return
    if not text or not text.strip():
        return
    values.add(text)
    stripped = text.strip()
    if stripped:
        values.add(stripped)
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    if escaped and escaped != text:
        values.add(escaped)

    def _collect_json_strings(value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                values.add(value)
            return
        if isinstance(value, dict):
            for item in value.values():
                _collect_json_strings(item)
            return
        if isinstance(value, list):
            for item in value:
                _collect_json_strings(item)

    def _collect_json_text(candidate: str) -> None:
        try:
            decoded = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        _collect_json_strings(decoded)

    for line in text.splitlines():
        line_value = line.strip()
        if line_value:
            values.add(line_value)
        for separator in ("=", ":"):
            if separator not in line_value:
                continue
            key, candidate = line_value.split(separator, 1)
            if not key.strip() or not candidate.strip():
                continue
            candidate = candidate.strip().strip("\"'")
            if candidate:
                values.add(candidate)
                _collect_json_text(candidate)
            break
    _collect_json_text(text)


def _collect_taint_values(value: Any, values: set[str], *, parent_key: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if normalized_key in _TAINT_VALUE_KEYS and isinstance(item, str | bytes):
                _add_taint_variants(values, item)
            else:
                _collect_taint_values(item, values, parent_key=normalized_key)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect_taint_values(item, values, parent_key=parent_key)
        return
    if parent_key in _TAINT_VALUE_KEYS:
        _add_taint_variants(values, value)


def collect_sensitive_response_taints(
    tool_name: str,
    arguments: Any,
    result: Any,
    *,
    boundary: SensitiveToolBoundary | None = None,
) -> tuple[str, ...]:
    """Collect exact approved content values that a provider must not echo durably.

    Paths, categories, operation names, and unrelated metadata are deliberately
    excluded. Only content-bearing leaves from a sensitive tool exchange become
    taints, which keeps redaction narrow and value-specific.
    """

    effective = boundary or sensitive_tool_boundary(tool_name, arguments, result=result)
    if not effective.sensitive:
        return ()
    values: set[str] = set()
    _collect_taint_values(arguments, values)
    if not (isinstance(result, dict) and "error" in result):
        if isinstance(result, str | bytes):
            _add_taint_variants(values, result)
        else:
            _collect_taint_values(result, values)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _redact_tainted_text(text: str, taints: tuple[str, ...]) -> str:
    redacted = text
    for taint in taints:
        if taint and taint in redacted:
            redacted = redacted.replace(taint, _REDACTED_RESPONSE_TAINT)
    return redacted


def _redact_tainted_value(value: Any, taints: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redacted = _redact_tainted_text(value, taints)
        return value if redacted == value else redacted
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="replace")
        redacted = _redact_tainted_text(decoded, taints)
        return value if redacted == decoded else redacted.encode("utf-8")
    if isinstance(value, dict):
        copied: dict[Any, Any] | None = None
        for key, item in value.items():
            redacted_item = _redact_tainted_value(item, taints)
            if redacted_item is not item and copied is None:
                copied = dict(value)
            if copied is not None:
                copied[key] = redacted_item
        return value if copied is None else copied
    if isinstance(value, list):
        redacted_items = [_redact_tainted_value(item, taints) for item in value]
        if all(
            redacted is original for redacted, original in zip(redacted_items, value, strict=True)
        ):
            return value
        return redacted_items
    if isinstance(value, tuple):
        redacted_items = tuple(_redact_tainted_value(item, taints) for item in value)
        if all(
            redacted is original for redacted, original in zip(redacted_items, value, strict=True)
        ):
            return value
        return redacted_items
    if is_dataclass(value) and not isinstance(value, type):
        updates: dict[str, Any] = {}
        for dataclass_field in fields(value):
            original = getattr(value, dataclass_field.name)
            redacted = _redact_tainted_value(original, taints)
            if redacted is not original:
                updates[dataclass_field.name] = redacted
        if not updates:
            return value
        try:
            return replace(value, **updates)
        except (TypeError, ValueError):
            return value
    return value


def redact_sensitive_response_taints(response: Any, taints: Collection[str]) -> Any:
    """Redact exact approved values from every provider-response channel."""

    normalized = tuple(
        sorted(
            {str(value) for value in taints if isinstance(value, str) and value},
            key=lambda item: (-len(item), item),
        )
    )
    if not normalized:
        return response
    redacted_response = _redact_tainted_value(response, normalized)
    if redacted_response is not response:
        return redacted_response

    content = _redact_tainted_value(str(getattr(response, "content", "") or ""), normalized)
    raw = _redact_tainted_value(getattr(response, "raw", {}), normalized)
    response_tool_calls = list(getattr(response, "tool_calls", []) or [])
    tool_calls: list[Any] = []
    calls_changed = False
    for tool_call in response_tool_calls:
        arguments = _redact_tainted_value(getattr(tool_call, "arguments", None), normalized)
        provider_metadata = _redact_tainted_value(
            getattr(tool_call, "provider_metadata", None), normalized
        )
        if arguments is getattr(tool_call, "arguments", None) and provider_metadata is getattr(
            tool_call, "provider_metadata", None
        ):
            tool_calls.append(tool_call)
            continue
        calls_changed = True
        tool_calls.append(
            _PersistenceSafeToolCall(
                id=str(getattr(tool_call, "id", "") or ""),
                name=str(getattr(tool_call, "name", "") or ""),
                arguments=arguments,
                provider_metadata=provider_metadata,
            )
        )
    provider_metadata = _redact_tainted_value(
        getattr(response, "provider_metadata", None), normalized
    )
    reasoning = _redact_tainted_value(getattr(response, "reasoning", ()), normalized)
    usage = _redact_tainted_value(getattr(response, "usage", None), normalized)
    changed = (
        content != str(getattr(response, "content", "") or "")
        or raw is not getattr(response, "raw", {})
        or calls_changed
        or provider_metadata is not getattr(response, "provider_metadata", None)
        or reasoning is not getattr(response, "reasoning", ())
        or usage is not getattr(response, "usage", None)
    )
    if not changed:
        return response
    return _PersistenceSafeResponse(
        content=content,
        tool_calls=tool_calls,
        raw=raw,
        response_model=copy.deepcopy(getattr(response, "response_model", None)),
        usage=usage,
        provider_metadata=provider_metadata,
        reasoning=reasoning,
        assistant_phase=copy.deepcopy(getattr(response, "assistant_phase", None)),
    )


def redact_assistant_tool_call_message(message: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive call bodies while preserving provider call IDs and shape."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message
    copied: dict[str, Any] | None = None
    copied_calls: list[Any] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            copied_calls.append(copy.deepcopy(call))
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            copied_calls.append(copy.deepcopy(call))
            continue
        tool_name = str(function.get("name") or "")
        serialized = function.get("arguments")
        try:
            arguments = json.loads(serialized) if isinstance(serialized, str) else serialized
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = {}
        boundary = sensitive_tool_boundary(tool_name, arguments)
        if not boundary.sensitive:
            copied_calls.append(copy.deepcopy(call))
            continue
        if copied is None:
            copied = copy.deepcopy(message)
        copied_call = copy.deepcopy(call)
        copied_function = dict(copied_call["function"])
        redacted = redact_sensitive_tool_arguments(
            tool_name,
            arguments,
            boundary=boundary,
        )
        copied_function["arguments"] = json.dumps(redacted, ensure_ascii=False)
        copied_call["function"] = copied_function
        copied_calls.append(copied_call)
    if copied is None:
        return message
    copied["content"] = ""
    copied["tool_calls"] = copied_calls
    return copied


def redact_sensitive_response_for_persistence(
    response: Any, *, taints: Collection[str] = ()
) -> Any:
    """Return a usage/persistence-safe response while preserving accounting fields."""

    response = redact_sensitive_response_taints(response, taints)

    tool_calls = getattr(response, "tool_calls", None)
    if not isinstance(tool_calls, list):
        return response
    copied_calls: list[Any] = []
    changed = False
    for tool_call in tool_calls:
        tool_name = str(getattr(tool_call, "name", "") or "")
        arguments = getattr(tool_call, "arguments", None)
        boundary = sensitive_tool_boundary(tool_name, arguments)
        if not boundary.sensitive:
            copied_calls.append(tool_call)
            continue
        changed = True
        redacted_arguments = redact_sensitive_tool_arguments(
            tool_name,
            arguments,
            boundary=boundary,
        )
        try:
            copied_calls.append(replace(tool_call, arguments=redacted_arguments))
        except (TypeError, ValueError):
            copied_calls.append(
                _PersistenceSafeToolCall(
                    id=str(getattr(tool_call, "id", "") or ""),
                    name=tool_name,
                    arguments=redacted_arguments,
                    provider_metadata=copy.deepcopy(getattr(tool_call, "provider_metadata", None)),
                )
            )
    if not changed:
        return response
    try:
        # Provider-native raw bodies commonly repeat function arguments. They
        # are not needed for usage accounting and cannot safely be retained.
        return replace(response, content="", tool_calls=copied_calls, raw={})
    except (TypeError, ValueError):
        return _PersistenceSafeResponse(
            content="",
            tool_calls=copied_calls,
            raw={},
            response_model=copy.deepcopy(getattr(response, "response_model", None)),
            usage=copy.deepcopy(getattr(response, "usage", None)),
            provider_metadata=copy.deepcopy(getattr(response, "provider_metadata", None)),
            reasoning=copy.deepcopy(getattr(response, "reasoning", ())),
            assistant_phase=copy.deepcopy(getattr(response, "assistant_phase", None)),
        )


def redact_consumed_sensitive_tool_messages(
    messages: list[dict[str, Any]],
    result_stubs: dict[str, str],
) -> None:
    """Scrub one-call-only sensitive messages after the provider consumed them."""

    if not result_stubs:
        return
    call_ids = set(result_stubs)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id in result_stubs:
                message["content"] = result_stubs[call_id]
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        if any(
            isinstance(call, dict) and str(call.get("id") or "") in call_ids for call in tool_calls
        ):
            messages[index] = redact_assistant_tool_call_message(message)


def inject_ephemeral_sensitive_tool_messages(
    messages: list[dict[str, Any]],
    *,
    result_content: dict[str, str],
    arguments_content: dict[str, str],
) -> list[dict[str, Any]]:
    """Expose sensitive tool material to one provider request, without mutating history."""

    call_ids = set(result_content) | set(arguments_content)
    if not call_ids:
        return messages
    injected = copy.deepcopy(messages)
    for message in injected:
        if str(message.get("role") or "") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id in result_content:
                message["content"] = result_content[call_id]
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            if call_id not in arguments_content:
                continue
            function = call.get("function")
            if isinstance(function, dict):
                function["arguments"] = arguments_content[call_id]
    return injected


__all__ = [
    "SensitiveToolBoundary",
    "collect_sensitive_response_taints",
    "inject_ephemeral_sensitive_tool_messages",
    "redact_assistant_tool_call_message",
    "redact_consumed_sensitive_tool_messages",
    "redact_sensitive_tool_arguments",
    "redact_sensitive_tool_result",
    "redact_sensitive_response_for_persistence",
    "redact_sensitive_response_taints",
    "sensitive_result_stub",
    "sensitive_tool_boundary",
]
