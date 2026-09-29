from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
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


ToolAvailabilitySnapshot = dict[str, ToolAvailability]


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


def _register_in(
    availability: MutableMapping[str, ToolAvailability],
    name: str,
    *,
    optional: bool,
) -> ToolAvailability:
    clean_name = _clean_tool_name(name)
    key = clean_name.casefold()
    existing = availability.get(key)
    if existing is None:
        state = ToolAvailability(name=clean_name, optional=bool(optional))
    else:
        state = replace(existing, name=clean_name, optional=bool(optional))
    if state.unavailable_reason and not state.optional:
        raise ToolSetupError(
            f"tool {state.name} is required but unavailable: {state.unavailable_reason}"
        )
    availability[key] = state
    return state


def register_tool_availability(
    name: str,
    *,
    optional: bool,
    availability: MutableMapping[str, ToolAvailability] | None = None,
) -> ToolAvailability:
    """Register a tool globally, or in one private build/turn snapshot.

    Omitting ``availability`` preserves the original process-global API for
    direct callers. Session and child runtimes pass a private mapping so their
    tool surfaces cannot overwrite one another while they are assembled.
    """

    if availability is not None:
        return _register_in(availability, name, optional=optional)
    with _LOCK:
        return _register_in(_AVAILABILITY_BY_NAME, name, optional=optional)


def _mark_available_in(
    availability: MutableMapping[str, ToolAvailability],
    name: str,
) -> ToolAvailability:
    clean_name = _clean_tool_name(name)
    key = clean_name.casefold()
    existing = availability.get(key)
    optional = existing.optional if existing is not None else True
    state = ToolAvailability(name=clean_name, optional=optional)
    availability[key] = state
    return state


def mark_available(
    name: str,
    *,
    availability: MutableMapping[str, ToolAvailability] | None = None,
) -> ToolAvailability:
    if availability is not None:
        return _mark_available_in(availability, name)
    with _LOCK:
        return _mark_available_in(_AVAILABILITY_BY_NAME, name)


def _mark_unavailable_in(
    availability: MutableMapping[str, ToolAvailability],
    name: str,
    reason: str,
    *,
    record_logged: bool,
) -> tuple[ToolAvailability, bool]:
    clean_name = _clean_tool_name(name)
    clean_reason = _clean_unavailable_reason(reason)
    key = clean_name.casefold()
    existing = availability.get(key)
    state = existing or ToolAvailability(name=clean_name, optional=True)
    if not state.optional:
        raise ToolSetupError(f"tool {state.name} is required but unavailable: {clean_reason}")
    should_log = not state.unavailable_logged
    state = replace(
        state,
        name=clean_name,
        unavailable_reason=clean_reason,
        unavailable_logged=(True if record_logged else state.unavailable_logged),
    )
    availability[key] = state
    return state, should_log


def mark_unavailable(
    name: str,
    reason: str,
    *,
    availability: MutableMapping[str, ToolAvailability] | None = None,
) -> ToolAvailability:
    if availability is not None:
        # Private build and turn snapshots do not emit process-global
        # availability logs. Build-time logging is deferred until a legacy
        # global publication actually commits; turn-local web failures already
        # have their own durable diagnostic event.
        state, _should_log = _mark_unavailable_in(
            availability,
            name,
            reason,
            record_logged=False,
        )
        return state
    with _LOCK:
        state, should_log = _mark_unavailable_in(
            _AVAILABILITY_BY_NAME,
            name,
            reason,
            record_logged=True,
        )
    if should_log:
        LOGGER.info(
            "optional_tool_unavailable tool=%s reason=%s",
            state.name,
            state.unavailable_reason,
        )
    return state


def tool_availability_snapshot(
    availability: Mapping[str, ToolAvailability] | None = None,
) -> ToolAvailabilitySnapshot:
    """Return a detached snapshot of a local mapping or the global fallback."""

    if availability is not None:
        return dict(availability)
    with _LOCK:
        return dict(_AVAILABILITY_BY_NAME)


def publish_tool_availability_snapshot(
    availability: Mapping[str, ToolAvailability],
) -> ToolAvailabilitySnapshot:
    """Atomically merge a completed direct build into the global fallback.

    Session-owned builds use a sink instead. This publication path exists for
    legacy/direct ``build_tools`` callers and preserves their process-global
    lookup behavior without exposing a partially assembled tool surface.
    """

    pending = tool_availability_snapshot(availability)
    log_states: list[ToolAvailability] = []
    with _LOCK:
        merged = dict(_AVAILABILITY_BY_NAME)
        for key, candidate in pending.items():
            if candidate.unavailable_reason and not candidate.optional:
                raise ToolSetupError(
                    f"tool {candidate.name} is required but unavailable: "
                    f"{candidate.unavailable_reason}"
                )
            existing = merged.get(key)
            should_log = bool(
                candidate.unavailable_reason
                and not candidate.unavailable_logged
                and not (existing is not None and existing.unavailable_logged)
            )
            committed = replace(
                candidate,
                unavailable_logged=bool(candidate.unavailable_reason),
            )
            merged[key] = committed
            if should_log:
                log_states.append(committed)
        _AVAILABILITY_BY_NAME.clear()
        _AVAILABILITY_BY_NAME.update(merged)
        committed_snapshot = dict(pending)
        for key in committed_snapshot:
            committed_snapshot[key] = _AVAILABILITY_BY_NAME[key]
    for state in log_states:
        LOGGER.info(
            "optional_tool_unavailable tool=%s reason=%s",
            state.name,
            state.unavailable_reason,
        )
    return committed_snapshot


def get_tool_availability(
    name: str,
    *,
    availability: Mapping[str, ToolAvailability] | None = None,
) -> ToolAvailability | None:
    key = _availability_key(name)
    if availability is not None:
        state = availability.get(key)
        if state is not None:
            return state
    with _LOCK:
        return _AVAILABILITY_BY_NAME.get(key)


def unavailable_tool_result(
    name: str,
    *,
    availability: Mapping[str, ToolAvailability] | None = None,
) -> ToolUnavailableResult | None:
    state = get_tool_availability(name, availability=availability)
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


def tool_unavailable_cause(value: Any) -> str:
    """What made a tool unavailable, for display next to the withdrawn call.

    Web observations lead with the model-facing "continue with the information
    already gathered" instruction; a transcript only needs the cause behind it
    (empty when none was recorded).
    """

    if not is_tool_unavailable_result(value):
        return ""
    reason = value["reason"].strip()
    if not reason.startswith(WEB_UNAVAILABLE_OBSERVATION):
        return reason
    cause = reason.removeprefix(WEB_UNAVAILABLE_OBSERVATION).strip()
    return cause.removeprefix("Cause:").strip()


def _reset_tool_availability_for_tests() -> None:
    with _LOCK:
        _AVAILABILITY_BY_NAME.clear()
