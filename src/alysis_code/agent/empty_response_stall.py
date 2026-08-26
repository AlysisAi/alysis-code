"""Bounded handling for a model endpoint that stops answering.

Some endpoints, under some contexts, start returning responses with no text and
no tool calls. Retrying the identical request does not help: the input that
produced the empty response is re-sent unchanged, so the endpoint produces
another empty response. Without a wall-clock bound the loop can run for hours
and then terminate as a failure even though the working tree already holds
finished work.

This module supplies the facts that bound that situation. It is pure and side
effect free; the clock is injectable so the time-based rules are testable
without sleeping.

* ``response_is_contentless`` — a response carrying neither text nor tool calls.
  A response with tool calls and no text is ordinary and is never counted.
* ``EmptyResponseStallTracker`` — consecutive contentless responses, how long
  the current contentless streak has lasted, how many recovery cycles the
  session has spent, and the total time attributed to empty-response handling.
  A stall is declared on *either* the count rule or the time rule, so a provider
  that hangs for minutes per empty call is caught without waiting for the count.
* ``compact_recent_tool_output`` — drops the most recent tool-output block from
  a message list so one recovery attempt re-issues against a different context
  instead of the same one. Roles and ``tool_call_id`` values are preserved, so
  the assistant/tool pairing every provider requires stays intact.

Nothing here inspects the task, the repository, or the model: the trigger is the
observed shape of provider responses.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..branding import env_get

# ---------------------------------------------------------------------------
# Policy (kill-switch mirrors the route-arbitration / evidence-v2 idiom)
# ---------------------------------------------------------------------------

DEFAULT_STALL_THRESHOLD = 3
DEFAULT_STALL_SECONDS = 300.0
DEFAULT_MAX_RECOVERY_CYCLES = 2
DEFAULT_HANDLING_BUDGET_SECONDS = 600.0

# Backoff before a recovery re-issue. Small and bounded: the point is to let a
# transient provider condition clear, not to wait out an outage.
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_MAX_SECONDS = 30.0

# Characters of each recent tool result kept when the block is compacted. Short
# results are below this and survive verbatim, so compaction only removes bulk.
DEFAULT_TOOL_OUTPUT_KEEP_CHARS = 200


def empty_response_stall_guard_enabled(cfg: Any | None) -> bool:
    """Kill-switch for bounded empty-response handling.

    ``ALYSIS_EMPTY_RESPONSE_STALL`` (off/0/false/no/disabled) wins over the
    config value; default is on. When off, no stall is ever declared and the
    legacy retry-then-terminate behaviour applies.
    """
    env_value = env_get("ALYSIS_EMPTY_RESPONSE_STALL")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "empty_response_stall_guard_enabled", True))


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if parsed <= 0 or parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return fallback
    return parsed


@dataclass(frozen=True)
class EmptyResponseStallPolicy:
    """Resolved thresholds for one session."""

    enabled: bool = True
    consecutive_threshold: int = DEFAULT_STALL_THRESHOLD
    stall_seconds: float = DEFAULT_STALL_SECONDS
    max_recovery_cycles: int = DEFAULT_MAX_RECOVERY_CYCLES
    handling_budget_seconds: float = DEFAULT_HANDLING_BUDGET_SECONDS

    def as_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "consecutive_threshold": self.consecutive_threshold,
            "stall_seconds": self.stall_seconds,
            "max_recovery_cycles": self.max_recovery_cycles,
            "handling_budget_seconds": self.handling_budget_seconds,
        }


def resolve_empty_response_stall_policy(cfg: Any | None) -> EmptyResponseStallPolicy:
    """Build the policy from config, falling back to defaults on bad values."""
    return EmptyResponseStallPolicy(
        enabled=empty_response_stall_guard_enabled(cfg),
        consecutive_threshold=_positive_int(
            getattr(cfg, "empty_response_stall_threshold", None),
            DEFAULT_STALL_THRESHOLD,
        ),
        stall_seconds=_positive_float(
            getattr(cfg, "empty_response_stall_seconds", None),
            DEFAULT_STALL_SECONDS,
        ),
        max_recovery_cycles=_positive_int(
            getattr(cfg, "empty_response_max_recovery_cycles", None),
            DEFAULT_MAX_RECOVERY_CYCLES,
        ),
        handling_budget_seconds=_positive_float(
            getattr(cfg, "empty_response_handling_budget_seconds", None),
            DEFAULT_HANDLING_BUDGET_SECONDS,
        ),
    )


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------


def response_is_contentless(response: Any) -> bool:
    """True when a model response carries neither text nor a tool call.

    A response with tool calls but no text is the ordinary shape of a working
    step and is never contentless. Reasoning traces are not content: a response
    that only thought is still one the runtime cannot act on.
    """
    if response is None:
        return True
    if getattr(response, "tool_calls", None):
        return False
    return not str(getattr(response, "content", "") or "").strip()


# ---------------------------------------------------------------------------
# Stall tracking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StallSignal:
    """Outcome of observing one model response."""

    stalled: bool
    trigger: str
    consecutive_contentless: int
    streak_seconds: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "stalled": self.stalled,
            "trigger": self.trigger,
            "consecutive_contentless": self.consecutive_contentless,
            "streak_seconds": round(self.streak_seconds, 3),
        }


@dataclass(frozen=True)
class RecoveryPlan:
    """Whether one more recovery cycle may be spent, and how long to back off."""

    allowed: bool
    reason: str
    cycle: int
    backoff_seconds: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "cycle": self.cycle,
            "backoff_seconds": round(self.backoff_seconds, 3),
        }


@dataclass
class EmptyResponseStallTracker:
    """Per-session state for empty-response detection and its time budget."""

    policy: EmptyResponseStallPolicy = field(default_factory=EmptyResponseStallPolicy)
    clock: Callable[[], float] = time.monotonic
    consecutive_contentless: int = 0
    recovery_cycles_used: int = 0
    handling_seconds: float = 0.0
    total_contentless_responses: int = 0
    _streak_started_at: float | None = None
    _billed_through: float | None = None

    def observe(self, *, contentless: bool) -> StallSignal:
        """Record one model response and report whether the turn has stalled."""
        now = float(self.clock())
        if not contentless:
            self._bill(now)
            self.consecutive_contentless = 0
            self._streak_started_at = None
            self._billed_through = None
            return StallSignal(
                stalled=False,
                trigger="",
                consecutive_contentless=0,
                streak_seconds=0.0,
            )

        self.total_contentless_responses += 1
        if self._streak_started_at is None:
            # Time before the first contentless response belongs to ordinary
            # work (a long tool run, a slow but answering model), so billing
            # for empty-response handling starts here, not earlier.
            self._streak_started_at = now
            self._billed_through = now
        self.consecutive_contentless += 1
        streak_seconds = max(0.0, now - self._streak_started_at)

        if not self.policy.enabled:
            return StallSignal(
                stalled=False,
                trigger="",
                consecutive_contentless=self.consecutive_contentless,
                streak_seconds=streak_seconds,
            )
        if self.consecutive_contentless >= self.policy.consecutive_threshold:
            trigger = "consecutive_contentless_responses"
        elif streak_seconds >= self.policy.stall_seconds:
            trigger = "contentless_streak_duration"
        else:
            trigger = ""
        return StallSignal(
            stalled=bool(trigger),
            trigger=trigger,
            consecutive_contentless=self.consecutive_contentless,
            streak_seconds=streak_seconds,
        )

    def plan_recovery(self) -> RecoveryPlan:
        """Decide whether one more compaction-recovery cycle may be spent."""
        self._bill(float(self.clock()))
        cycle = self.recovery_cycles_used + 1
        if not self.policy.enabled:
            return RecoveryPlan(
                allowed=False,
                reason="stall_guard_disabled",
                cycle=cycle,
                backoff_seconds=0.0,
            )
        if self.recovery_cycles_used >= self.policy.max_recovery_cycles:
            return RecoveryPlan(
                allowed=False,
                reason="recovery_cycles_exhausted",
                cycle=cycle,
                backoff_seconds=0.0,
            )
        if self.handling_seconds >= self.policy.handling_budget_seconds:
            return RecoveryPlan(
                allowed=False,
                reason="handling_budget_exhausted",
                cycle=cycle,
                backoff_seconds=0.0,
            )
        return RecoveryPlan(
            allowed=True,
            reason="recovery_available",
            cycle=cycle,
            backoff_seconds=self._backoff_seconds(cycle),
        )

    def note_recovery_started(self, *, backoff_seconds: float = 0.0) -> None:
        """Charge one recovery cycle (plus the backoff actually slept)."""
        self.recovery_cycles_used += 1
        self.handling_seconds += max(0.0, float(backoff_seconds))
        self.consecutive_contentless = 0
        self._streak_started_at = None
        self._billed_through = None

    def remaining_budget_seconds(self) -> float:
        return max(0.0, self.policy.handling_budget_seconds - self.handling_seconds)

    def as_payload(self) -> dict[str, Any]:
        return {
            "consecutive_contentless": self.consecutive_contentless,
            "contentless_responses": self.total_contentless_responses,
            "recovery_cycles_used": self.recovery_cycles_used,
            "max_recovery_cycles": self.policy.max_recovery_cycles,
            "handling_seconds": round(self.handling_seconds, 3),
            "handling_budget_seconds": self.policy.handling_budget_seconds,
        }

    def _bill(self, now: float) -> None:
        if self._billed_through is None:
            return
        self.handling_seconds += max(0.0, now - self._billed_through)
        self._billed_through = now

    def _backoff_seconds(self, cycle: int) -> float:
        exponent = max(0, cycle - 1)
        return min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2**exponent))


# ---------------------------------------------------------------------------
# Context compaction for the recovery re-issue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolOutputCompaction:
    """What ``compact_recent_tool_output`` actually removed."""

    applied: bool
    compacted_messages: int
    removed_characters: int
    block_size: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "compacted_messages": self.compacted_messages,
            "removed_characters": self.removed_characters,
            "block_size": self.block_size,
        }


def _is_tool_message(message: Any) -> bool:
    return isinstance(message, Mapping) and str(message.get("role") or "") == "tool"


def _elision_note(removed: int) -> str:
    return (
        f"\n[Tool output elided by the runtime after repeated empty model responses: "
        f"{removed} characters removed. Re-run the tool if you still need this output.]"
    )


def compact_recent_tool_output(
    messages: Iterable[Mapping[str, Any]],
    *,
    keep_chars: int = DEFAULT_TOOL_OUTPUT_KEEP_CHARS,
) -> tuple[list[Any], ToolOutputCompaction]:
    """Drop the most recent tool-output block, keeping the message protocol valid.

    The block is the trailing run of ``role="tool"`` messages, located by
    scanning back from the end past any controller messages appended after it.
    Each oversized result keeps its first ``keep_chars`` characters plus an
    explicit note that the rest was removed, so the model is never told a
    truncated result is complete. Roles and ``tool_call_id`` values are
    untouched, so every assistant tool call keeps its matching result.
    """
    original = [dict(message) if isinstance(message, Mapping) else message for message in messages]
    keep = max(0, int(keep_chars))

    last_tool_index: int | None = None
    for index in range(len(original) - 1, -1, -1):
        if _is_tool_message(original[index]):
            last_tool_index = index
            break
    if last_tool_index is None:
        return original, ToolOutputCompaction(
            applied=False,
            compacted_messages=0,
            removed_characters=0,
            block_size=0,
        )

    start = last_tool_index
    while start - 1 >= 0 and _is_tool_message(original[start - 1]):
        start -= 1

    compacted_messages = 0
    removed_characters = 0
    for index in range(start, last_tool_index + 1):
        message = original[index]
        content = str(message.get("content") or "")
        if len(content) <= keep:
            continue
        removed = len(content) - keep
        message["content"] = content[:keep] + _elision_note(removed)
        compacted_messages += 1
        removed_characters += removed

    return original, ToolOutputCompaction(
        applied=compacted_messages > 0,
        compacted_messages=compacted_messages,
        removed_characters=removed_characters,
        block_size=last_tool_index - start + 1,
    )
