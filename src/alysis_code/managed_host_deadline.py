from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

DEFAULT_MANAGED_HOST_SHUTDOWN_RESERVE_SECONDS = 30.0
MINIMUM_MANAGED_HOST_INVOCATION_SECONDS = 1.0


class ManagedHostDeadlineError(ValueError):
    def __init__(self, code: str, message: str, *, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.record = record


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
