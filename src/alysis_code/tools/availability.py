from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Literal, TypedDict, TypeGuard

LOGGER = logging.getLogger(__name__)


class ToolUnavailableResult(TypedDict):
    """Structured non-error result returned when an optional tool is unavailable."""

    status: Literal["tool_unavailable"]
    tool: str
    reason: str


WEB_TOOL_NAMES = frozenset({"web_fetch", "web_search"})
WEB_UNAVAILABLE_OBSERVATION = (
    "Web tools are unavailable for the rest of this turn — "
    "continue with the information already gathered."
)

# A web-tool failure is *recoverable* when the model can fix it by changing its own
# arguments (input validation, provenance rejection with retry guidance). Those are
# returned to the model as plain errors; only unrecoverable failures (backend
# connectivity/availability after fallback exhaustion) disable web tools for the turn.
_WEB_RECOVERY_HINT_KEYS = ("guidance", "error_code", "fetchable_urls", "recoverable")


def is_recoverable_web_tool_error(error: BaseException) -> bool:
    """True when the exception marks itself as fixable by a corrected retry."""

    return bool(getattr(error, "recoverable", False))


def is_recoverable_web_error_result(value: Any) -> bool:
    """True for structured web-tool error dicts that carry retry/recovery guidance."""

    if not isinstance(value, dict) or "error" not in value:
        return False
    return any(key in value for key in _WEB_RECOVERY_HINT_KEYS)


@dataclass(frozen=True)
class ToolAvailability:
    name: str
    optional: bool
    unavailable_reason: str | None = None
    unavailable_logged: bool = False


class ToolSetupError(RuntimeError):
    """Raised when a required tool is unavailable at startup."""


_AVAILABILITY_BY_NAME: dict[str, ToolAvailability] = {}
_LOCK = RLock()


def _clean_tool_name(name: str) -> str:
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("tool name must be non-empty")
    return clean


def _availability_key(name: str) -> str:
    return _clean_tool_name(name).casefold()


def _clean_unavailable_reason(reason: str) -> str:
    clean = str(reason or "").strip()
    if not clean or clean.casefold() == "unavailable":
        raise ValueError("tool unavailable reason must be concrete and non-empty")
    return clean


def register_tool_availability(name: str, *, optional: bool) -> ToolAvailability:
    clean_name = _clean_tool_name(name)
    key = clean_name.casefold()
    with _LOCK:
        existing = _AVAILABILITY_BY_NAME.get(key)
        if existing is None:
            state = ToolAvailability(name=clean_name, optional=bool(optional))
        else:
            state = replace(existing, name=clean_name, optional=bool(optional))
        _AVAILABILITY_BY_NAME[key] = state
        if state.unavailable_reason and not state.optional:
            raise ToolSetupError(
                f"tool {state.name} is required but unavailable: {state.unavailable_reason}"
            )
        return state


def mark_available(name: str) -> ToolAvailability:
    clean_name = _clean_tool_name(name)
    key = clean_name.casefold()
    with _LOCK:
        existing = _AVAILABILITY_BY_NAME.get(key)
        optional = existing.optional if existing is not None else True
        state = ToolAvailability(name=clean_name, optional=optional)
        _AVAILABILITY_BY_NAME[key] = state
        return state


def mark_unavailable(name: str, reason: str) -> ToolAvailability:
    clean_name = _clean_tool_name(name)
    clean_reason = _clean_unavailable_reason(reason)
    key = clean_name.casefold()
    with _LOCK:
        existing = _AVAILABILITY_BY_NAME.get(key)
        state = existing or ToolAvailability(name=clean_name, optional=True)
        if not state.optional:
            raise ToolSetupError(f"tool {state.name} is required but unavailable: {clean_reason}")
        should_log = not state.unavailable_logged
        state = replace(
            state,
            name=clean_name,
            unavailable_reason=clean_reason,
            unavailable_logged=True,
        )
        _AVAILABILITY_BY_NAME[key] = state
    if should_log:
        LOGGER.info(
            "optional_tool_unavailable tool=%s reason=%s",
            state.name,
            state.unavailable_reason,
        )
    return state


def get_tool_availability(name: str) -> ToolAvailability | None:
    key = _availability_key(name)
    with _LOCK:
        return _AVAILABILITY_BY_NAME.get(key)


def unavailable_tool_result(name: str) -> ToolUnavailableResult | None:
    state = get_tool_availability(name)
    if state is None or not state.optional or not state.unavailable_reason:
        return None
    return {
        "status": "tool_unavailable",
        "tool": state.name,
        "reason": state.unavailable_reason,
    }


def web_unavailable_result(name: str, *, detail: str | None = None) -> ToolUnavailableResult:
    """Return a non-error observation for a failed optional web tool.

    ``detail`` carries the underlying failure summary so the model (and the
    user reading the transcript) can see *why* web tools were disabled — e.g.
    a configuration remedy — instead of a silent generic notice.
    """

    clean_name = _clean_tool_name(name)
    if clean_name.casefold() not in WEB_TOOL_NAMES:
        raise ValueError(f"not a web tool: {clean_name}")
    reason = WEB_UNAVAILABLE_OBSERVATION
    clean_detail = str(detail or "").strip()
    if clean_detail:
        reason = f"{WEB_UNAVAILABLE_OBSERVATION} Cause: {clean_detail}"
    return {
        "status": "tool_unavailable",
        "tool": clean_name,
        "reason": reason,
    }


def is_tool_unavailable_result(value: Any) -> TypeGuard[ToolUnavailableResult]:
    if not isinstance(value, dict):
        return False
    return (
        value.get("status") == "tool_unavailable"
        and isinstance(value.get("tool"), str)
        and bool(str(value.get("tool") or "").strip())
        and isinstance(value.get("reason"), str)
        and bool(str(value.get("reason") or "").strip())
        and "error" not in value
    )


def _reset_tool_availability_for_tests() -> None:
    with _LOCK:
        _AVAILABILITY_BY_NAME.clear()
