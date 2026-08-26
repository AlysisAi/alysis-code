"""Per-turn controller intervention telemetry.

The headline target is controller_interventions_total ≈ 0 on clean successful turns.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

HEADLINE_EXCLUDED_CLASSES = frozenset(
    {
        "context_setup",
        "deadline_directive",
        "safety_block",
    }
)


class ControllerInterventionTracker:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._counts: Counter[str] = Counter()
        self._headline_total = 0

    @property
    def headline_total(self) -> int:
        return self._headline_total

    def record(
        self,
        intervention_class: str,
        detail: str,
        *,
        step: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        headline_counted: bool | None = None,
    ) -> None:
        normalized_class = str(intervention_class or "").strip() or "other"
        normalized_detail = str(detail or "").strip() or "unspecified"
        counted = (
            normalized_class not in HEADLINE_EXCLUDED_CLASSES
            if headline_counted is None
            else bool(headline_counted)
        )
        self._counts[normalized_class] += 1
        if counted:
            self._headline_total += 1
        payload: dict[str, Any] = {
            "class": normalized_class,
            "detail": normalized_detail,
            "step": step,
            "headline_counted": counted,
            "controller_interventions_total": self._headline_total,
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        self._store.append("controller_intervention", payload)

    def payload(self) -> dict[str, Any]:
        return {
            "total": self._headline_total,
            "by_class": dict(sorted(self._counts.items())),
        }
