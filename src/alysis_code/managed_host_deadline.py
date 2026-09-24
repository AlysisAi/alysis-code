from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .execution_deadline import ExecutionDeadline

DEFAULT_MANAGED_HOST_SHUTDOWN_RESERVE_SECONDS = 30.0
MINIMUM_MANAGED_HOST_INVOCATION_SECONDS = 1.0
MANAGED_HOST_DEADLINE_UNIX_ENV = "ALYSIS_MANAGED_HOST_DEADLINE_UNIX_SECONDS"


class ManagedHostDeadlineError(ValueError):
    def __init__(self, code: str, message: str, *, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.record = record


def managed_host_deadline_anchor_unix_seconds(
    invocation_seconds: Any,
    *,
    existing_anchor: Any = None,
    now_unix_seconds: float | None = None,
) -> float:
    """Anchor a launch budget without renewing an inherited host expiry.

    Managed hosts and their execution environments must have synchronized wall
    clocks. The recipient converts this boundary to its monotonic clock once.
    An expired inherited boundary remains expired; malformed input fails closed.
    """
    record = {"schema_version": 1, "status": "blocked"}
    duration = _coerce_required_positive_finite(
        invocation_seconds, key="invocation_seconds", record=record
    )
    now = _coerce_required_positive_finite(
        time.time() if now_unix_seconds is None else now_unix_seconds,
        key="now_unix_seconds",
        record=record,
    )
    anchor = _coerce_required_positive_finite(
        now + duration, key="managed_host_deadline_unix_seconds", record=record
    )
    if existing_anchor is not None:
        anchor = min(
            anchor,
            _coerce_required_positive_finite(
                existing_anchor, key="managed_host_deadline_unix_seconds", record=record
            ),
        )
    return anchor


def clamp_to_managed_host_deadline(
    deadline: ExecutionDeadline,
    anchor_unix_seconds: Any,
    *,
    now_unix_seconds: float | None = None,
) -> dict[str, Any]:
    """Narrow the existing shared deadline once, including time before startup.

    No new timer or ongoing wall-clock dependency is introduced. Shifting the
    start by the same amount preserves the original budget span and counts
    startup against phased degradation. Existing policies and observations stay
    on the same shared deadline object, and an earlier parent bound always wins.
    """
    record = {"schema_version": 1, "status": "blocked"}
    anchor = _coerce_required_positive_finite(
        anchor_unix_seconds, key="managed_host_deadline_unix_seconds", record=record
    )
    # Sample monotonic first so conversion overhead can only narrow the bound.
    now_monotonic = float(deadline._clock())
    now_unix = _coerce_required_positive_finite(
        time.time() if now_unix_seconds is None else now_unix_seconds,
        key="now_unix_seconds",
        record=record,
    )
    previous_end = deadline.deadline_monotonic
    if previous_end is None:
        raise ManagedHostDeadlineError(
            "run_deadline_missing",
            "managed-host absolute expiry requires a finite run deadline",
            record={**record, "validation_error": "run_deadline_missing"},
        )
    effective_end = min(previous_end, now_monotonic + (anchor - now_unix))
    narrowed_by = previous_end - effective_end
    deadline.deadline_monotonic = effective_end
    deadline.started_at_monotonic -= narrowed_by
    return {
        "schema_version": 1,
        "status": "exhausted" if effective_end <= now_monotonic else "ok",
        "anchor_source": MANAGED_HOST_DEADLINE_UNIX_ENV,
        "anchor_unix_seconds": anchor,
        "converted_at_unix_seconds": now_unix,
        "converted_at_monotonic": now_monotonic,
        "deadline_source": str(getattr(deadline.source, "value", deadline.source)),
        "previous_deadline_monotonic": previous_end,
        "effective_deadline_monotonic": effective_end,
        "narrowed_by_seconds": narrowed_by,
    }


@dataclass(frozen=True)
class ManagedHostDeadline:
    timeout_source: str
    reserve_source: str
    final_effective_host_agent_timeout_seconds: float
    elapsed_before_launch_seconds: float
    host_shutdown_reserve_seconds: float
    alysis_invocation_deadline_seconds: float
    require_deadline: bool = True
    status: str = "ok"
    schema_version: int = 1

    @property
    def host_remaining_timeout_seconds(self) -> float:
        return self.final_effective_host_agent_timeout_seconds - self.elapsed_before_launch_seconds

    def diagnostic_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "timeout_source": self.timeout_source,
            "reserve_source": self.reserve_source,
            "final_effective_host_agent_timeout_seconds": _round_seconds(
                self.final_effective_host_agent_timeout_seconds
            ),
            "elapsed_before_launch_seconds": _round_seconds(self.elapsed_before_launch_seconds),
            "host_shutdown_reserve_seconds": _round_seconds(self.host_shutdown_reserve_seconds),
            "alysis_invocation_deadline_seconds": _round_seconds(
                self.alysis_invocation_deadline_seconds
            ),
            "require_deadline": self.require_deadline,
        }


def resolve_managed_host_deadline(
    *,
    final_effective_host_agent_timeout_seconds: Any,
    host_shutdown_reserve_seconds: Any = DEFAULT_MANAGED_HOST_SHUTDOWN_RESERVE_SECONDS,
    elapsed_before_launch_seconds: Any = 0.0,
    timeout_source: str = "managed_host",
    reserve_source: str = "default",
    require_deadline: bool = True,
    minimum_invocation_seconds: float = MINIMUM_MANAGED_HOST_INVOCATION_SECONDS,
) -> ManagedHostDeadline:
    base_record = {
        "schema_version": 1,
        "status": "blocked",
        "timeout_source": timeout_source,
        "reserve_source": reserve_source,
        "require_deadline": require_deadline,
    }
    host_timeout = _coerce_required_positive_finite(
        final_effective_host_agent_timeout_seconds,
        key="final_effective_host_agent_timeout_seconds",
        record=base_record,
    )
    reserve = _coerce_non_negative_finite(
        host_shutdown_reserve_seconds,
        key="host_shutdown_reserve_seconds",
        record={
            **base_record,
            "final_effective_host_agent_timeout_seconds": _round_seconds(host_timeout),
        },
    )
    elapsed = _coerce_non_negative_finite(
        elapsed_before_launch_seconds,
        key="elapsed_before_launch_seconds",
        record={
            **base_record,
            "final_effective_host_agent_timeout_seconds": _round_seconds(host_timeout),
            "host_shutdown_reserve_seconds": _round_seconds(reserve),
        },
    )
    minimum = _coerce_required_positive_finite(
        minimum_invocation_seconds,
        key="minimum_invocation_seconds",
        record={
            **base_record,
            "final_effective_host_agent_timeout_seconds": _round_seconds(host_timeout),
            "host_shutdown_reserve_seconds": _round_seconds(reserve),
            "elapsed_before_launch_seconds": _round_seconds(elapsed),
        },
    )
    if reserve >= host_timeout:
        record = {
            **base_record,
            "final_effective_host_agent_timeout_seconds": _round_seconds(host_timeout),
            "host_shutdown_reserve_seconds": _round_seconds(reserve),
            "elapsed_before_launch_seconds": _round_seconds(elapsed),
            "validation_error": "host_shutdown_reserve_consumes_timeout",
        }
        raise ManagedHostDeadlineError(
            "host_shutdown_reserve_consumes_timeout",
            "host shutdown reserve must be smaller than the final effective host agent timeout",
            record=record,
        )

    invocation_deadline = host_timeout - elapsed - reserve
    if invocation_deadline < minimum:
        record = {
            **base_record,
            "final_effective_host_agent_timeout_seconds": _round_seconds(host_timeout),
            "host_shutdown_reserve_seconds": _round_seconds(reserve),
            "elapsed_before_launch_seconds": _round_seconds(elapsed),
            "alysis_invocation_deadline_seconds": _round_seconds(max(0.0, invocation_deadline)),
            "minimum_invocation_seconds": _round_seconds(minimum),
            "validation_error": "remaining_duration_too_small",
        }
        raise ManagedHostDeadlineError(
            "remaining_duration_too_small",
            "remaining managed-host duration is too small to launch Alysis Code safely",
            record=record,
        )

    return ManagedHostDeadline(
        timeout_source=timeout_source,
        reserve_source=reserve_source,
        final_effective_host_agent_timeout_seconds=host_timeout,
        elapsed_before_launch_seconds=elapsed,
        host_shutdown_reserve_seconds=reserve,
        alysis_invocation_deadline_seconds=invocation_deadline,
        require_deadline=require_deadline,
    )


def _coerce_required_positive_finite(
    value: Any,
    *,
    key: str,
    record: dict[str, Any],
) -> float:
    if value is None:
        raise ManagedHostDeadlineError(
            f"{key}_missing",
            f"{key} is required for managed-host deadline enforcement",
            record={**record, "validation_error": f"{key}_missing"},
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ManagedHostDeadlineError(
            f"{key}_invalid",
            f"{key} must be a finite number > 0",
            record={**record, "validation_error": f"{key}_invalid"},
        ) from exc
    if parsed <= 0 or not math.isfinite(parsed):
        raise ManagedHostDeadlineError(
            f"{key}_invalid",
            f"{key} must be a finite number > 0",
            record={**record, "validation_error": f"{key}_invalid"},
        )
    return parsed


def _coerce_non_negative_finite(
    value: Any,
    *,
    key: str,
    record: dict[str, Any],
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ManagedHostDeadlineError(
            f"{key}_invalid",
            f"{key} must be a finite number >= 0",
            record={**record, "validation_error": f"{key}_invalid"},
        ) from exc
    if parsed < 0 or not math.isfinite(parsed):
        raise ManagedHostDeadlineError(
            f"{key}_invalid",
            f"{key} must be a finite number >= 0",
            record={**record, "validation_error": f"{key}_invalid"},
        )
    return parsed


def _round_seconds(value: float) -> float:
    return round(float(value), 6)
