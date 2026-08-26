"""The lifecycle state of a workspace's current Forge run.

``.alysis/current_run.json`` used to be a pure pointer: which run directory is
current, and how the workspace was bound. That left every surface guessing at the
one thing users actually ask -- *is this run going, finished, or dead?* -- and a
crashed process left behind a pointer indistinguishable from a healthy one, so
UIs showed a phantom "active" run forever.

This module owns the answer. The pointer now carries an explicit ``status`` drawn
from a closed enum:

``draft``
    A run exists and its plan is not execution-ready yet.
``approved``
    The plan passes the same acceptance gate ``forge exec`` applies. Executable.
``running``
    A process is executing the plan right now, and says which process.
``interrupted``
    Execution stopped without reaching a terminal state. Resumable.
``completed`` / ``failed``
    Terminal. Execution finished; the work was, or was not, accepted.

``interrupted`` is the state that makes crash recovery honest: nothing writes it
during a normal run, so it can only be reached by
:func:`reconcile_status_after_crash` observing ``running`` with no live lock.

The module is pure -- dict in, dict out, no filesystem and no imports from
:mod:`alysis_code.forge` -- so both the pointer writer and the lock layer
can depend on it without a cycle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

RUN_STATE_SCHEMA_VERSION = 1

RUN_STATUS_DRAFT = "draft"
RUN_STATUS_APPROVED = "approved"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_INTERRUPTED = "interrupted"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

RUN_STATUSES: tuple[str, ...] = (
    RUN_STATUS_DRAFT,
    RUN_STATUS_APPROVED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_INTERRUPTED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
)

# Statuses whose work is unfinished and can be picked back up by `forge resume`.
# ``failed`` is included on purpose: a run that stopped on a rejected task is
# exactly the run a retry flag exists for.
RESUMABLE_RUN_STATUSES: frozenset[str] = frozenset({RUN_STATUS_INTERRUPTED, RUN_STATUS_FAILED})
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({RUN_STATUS_COMPLETED, RUN_STATUS_FAILED})

STATUS_KEY = "status"
STATUS_UPDATED_AT_KEY = "status_updated_at"
STATUS_REASON_KEY = "status_reason"
STATUS_HISTORY_KEY = "status_history"
RUN_OWNER_KEY = "run_owner"
PLAN_FINGERPRINT_KEY = "plan_fingerprint"

# Bounded so a workspace that starts and stops a run a thousand times does not grow
# an unbounded pointer file. The tail is what diagnoses a crash; the head is noise.
STATUS_HISTORY_LIMIT = 24

PLAN_FINGERPRINT_SCHEMA_VERSION = 1

# The plan's *authored definition*. Deliberately excludes everything execution is
# allowed to move on its own -- ``status``, ``attempts``, ``branch`` -- so a run's
# own progress can never read as someone having edited the plan underneath it.
_TASK_DEFINITION_KEYS: tuple[str, ...] = (
    "title",
    "description",
    "acceptance_criteria",
    "dependencies",
    "estimated_files",
    "write_scope",
    "task_kind",
    "mcp_scope",
)

_TASK_FIELD_LABELS: dict[str, str] = {
    "title": "title",
    "description": "description",
    "acceptance_criteria": "acceptance criteria",
    "dependencies": "dependencies",
    "estimated_files": "estimated files",
    "write_scope": "write scope",
    "task_kind": "task kind",
    "mcp_scope": "MCP scope",
}


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_run_status(value: Any) -> str | None:
    """Return the enum member ``value`` names, or ``None`` if it names none."""
    text = str(value or "").strip().lower()
    return text if text in RUN_STATUSES else None


def pointer_status(pointer: Mapping[str, Any] | None) -> str:
    """The recorded status, defaulting to ``draft``.

    Pointers written before this schema carry no status at all. ``draft`` is the
    honest default for them: it is the only value that claims nothing about work
    this build never observed.
    """
    if pointer is None:
        return RUN_STATUS_DRAFT
    return normalize_run_status(pointer.get(STATUS_KEY)) or RUN_STATUS_DRAFT


def status_is_resumable(status: str) -> bool:
    return (normalize_run_status(status) or "") in RESUMABLE_RUN_STATUSES


def status_is_terminal(status: str) -> bool:
    return (normalize_run_status(status) or "") in TERMINAL_RUN_STATUSES


def describe_run_status(status: str) -> str:
    normalized = normalize_run_status(status) or RUN_STATUS_DRAFT
    return {
        RUN_STATUS_DRAFT: "plan is not execution-ready yet",
        RUN_STATUS_APPROVED: "plan is execution-ready and not started",
        RUN_STATUS_RUNNING: "a process is executing this plan",
        RUN_STATUS_INTERRUPTED: "execution stopped without finishing",
        RUN_STATUS_COMPLETED: "execution finished and the work was accepted",
        RUN_STATUS_FAILED: "execution finished and the work was not accepted",
    }[normalized]


def build_run_owner(
    *,
    pid: int,
    hostname: str,
    mode: str,
    started_at: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """The identity a ``running`` pointer publishes about its executing process."""
    return {
        "pid": int(pid),
        "hostname": str(hostname),
        "mode": str(mode),
        "started_at": started_at or _now_iso(),
        "session_id": str(session_id) if session_id else None,
    }


def apply_run_status(
    pointer: Mapping[str, Any] | None,
    status: str,
    *,
    reason: str = "",
    owner: Mapping[str, Any] | None = None,
    plan_fingerprint: Mapping[str, Any] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Return ``pointer`` with ``status`` applied and the transition recorded.

    Pure: the caller decides whether the result is worth writing. ``owner`` is
    cleared on any status other than ``running`` -- a stale owner block on a
    finished run is the same phantom-active problem in a different field.
    """
    normalized = normalize_run_status(status)
    if normalized is None:
        raise ValueError(
            f"unknown run status {status!r}; expected one of {', '.join(RUN_STATUSES)}"
        )
    stamp = at or _now_iso()
    updated: dict[str, Any] = dict(pointer or {})
    previous = pointer_status(pointer) if pointer else None

    updated[STATUS_KEY] = normalized
    updated[STATUS_UPDATED_AT_KEY] = stamp
    if reason:
        updated[STATUS_REASON_KEY] = reason
    else:
        updated.pop(STATUS_REASON_KEY, None)

    if normalized == RUN_STATUS_RUNNING:
        if owner is not None:
            updated[RUN_OWNER_KEY] = dict(owner)
    else:
        updated.pop(RUN_OWNER_KEY, None)

    if plan_fingerprint is not None:
        updated[PLAN_FINGERPRINT_KEY] = dict(plan_fingerprint)

    history = list(updated.get(STATUS_HISTORY_KEY) or [])
    entry: dict[str, Any] = {"status": normalized, "at": stamp}
    if previous and previous != normalized:
        entry["from"] = previous
    if reason:
        entry["reason"] = reason
    history.append(entry)
    updated[STATUS_HISTORY_KEY] = history[-STATUS_HISTORY_LIMIT:]
    return updated


def reconcile_status_after_crash(
    pointer: Mapping[str, Any] | None,
    *,
    lock_is_live: bool,
    at: str | None = None,
) -> dict[str, Any] | None:
    """Transition a crash leftover to ``interrupted``, or return ``None``.

    ``running`` with no live lock is the crash signature: the executing process
    holds the workspace lock for its whole run and releases it on the way out, so a
    pointer claiming ``running`` while nothing holds the lock describes a process
    that is not there. Every other combination is left exactly as it is -- this
    function's only job is to stop UIs from showing a run that nobody is running.
    """
    if pointer is None:
        return None
    if pointer_status(pointer) != RUN_STATUS_RUNNING:
        return None
    if lock_is_live:
        return None
    owner = pointer.get(RUN_OWNER_KEY)
    detail = ""
    if isinstance(owner, Mapping):
        pid = owner.get("pid")
        host = owner.get("hostname")
        if pid and host:
            detail = f" (owner pid {pid} on {host} is gone)"
    return apply_run_status(
        pointer,
        RUN_STATUS_INTERRUPTED,
        reason=f"run was marked running but no execution holds the workspace lock{detail}",
        at=at,
    )


# ---------------------------------------------------------------------------
# Plan fingerprints
# ---------------------------------------------------------------------------


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _task_id(task: Mapping[str, Any]) -> str:
    return str(task.get("id") or "").strip()


def _plan_tasks(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tasks = plan.get("tasks")
    if not isinstance(tasks, Sequence):
        return []
    return [task for task in tasks if isinstance(task, Mapping) and _task_id(task)]


def _requirement_texts(plan: Mapping[str, Any]) -> list[str]:
    raw = plan.get("requirements")
    if not isinstance(raw, Sequence):
        return []
    texts: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            for key in ("text", "requirement", "title", "description", "content"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
                    break
        elif str(item).strip():
            texts.append(str(item).strip())
    return texts


def plan_fingerprint(plan: Mapping[str, Any]) -> dict[str, Any]:
    """A comparable digest of everything about a plan that a human authored.

    Field-level rather than whole-file, so drift can be reported as *what* changed
    ("T02 acceptance criteria changed") instead of the useless "the plan changed".
    """
    task_digests = {
        _task_id(task): {
            key: _digest(task.get(key)) for key in _TASK_DEFINITION_KEYS if key in task
        }
        for task in _plan_tasks(plan)
    }
    payload = {
        "schema_version": PLAN_FINGERPRINT_SCHEMA_VERSION,
        "project_goal": _digest(str(plan.get("project_goal") or "").strip()),
        "summary": _digest(str(plan.get("summary") or "").strip()),
        "requirements": _digest(_requirement_texts(plan)),
        "task_order": [_task_id(task) for task in _plan_tasks(plan)],
        "tasks": task_digests,
    }
    payload["digest"] = _digest(
        {key: value for key, value in payload.items() if key != "schema_version"}
    )
    return payload


@dataclass(frozen=True)
class PlanDrift:
    """What changed between the plan a run was approved against and today's plan."""

    changed: bool
    goal_changed: bool = False
    summary_changed: bool = False
    requirements_changed: bool = False
    order_changed: bool = False
    tasks_added: tuple[str, ...] = ()
    tasks_removed: tuple[str, ...] = ()
    tasks_changed: tuple[tuple[str, tuple[str, ...]], ...] = ()
    comparable: bool = True

    @property
    def reasons(self) -> tuple[str, ...]:
        """One human line per distinct change, for the re-approval prompt."""
        if not self.comparable:
            return ("this run has no recorded plan fingerprint to compare against",)
        lines: list[str] = []
        if self.goal_changed:
            lines.append("project goal changed")
        if self.summary_changed:
            lines.append("plan summary changed")
        if self.requirements_changed:
            lines.append("requirements changed")
        if self.tasks_added:
            lines.append(f"tasks added: {', '.join(self.tasks_added)}")
        if self.tasks_removed:
            lines.append(f"tasks removed: {', '.join(self.tasks_removed)}")
        for task_id, fields in self.tasks_changed:
            labels = ", ".join(_TASK_FIELD_LABELS.get(name, name) for name in fields)
            lines.append(f"{task_id} changed: {labels}")
        if self.order_changed and not (self.tasks_added or self.tasks_removed):
            lines.append("task order changed")
        return tuple(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "comparable": self.comparable,
            "goal_changed": self.goal_changed,
            "summary_changed": self.summary_changed,
            "requirements_changed": self.requirements_changed,
            "order_changed": self.order_changed,
            "tasks_added": list(self.tasks_added),
            "tasks_removed": list(self.tasks_removed),
            "tasks_changed": [
                {"task_id": task_id, "fields": list(fields)}
                for task_id, fields in self.tasks_changed
            ],
            "reasons": list(self.reasons),
        }


def compare_plan_fingerprints(
    recorded: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> PlanDrift:
    """Diff a stored fingerprint against a freshly computed one.

    A missing or unreadable stored fingerprint is reported as *not comparable*
    rather than as "no drift": a run whose baseline was never recorded has not been
    shown to be unchanged, and resume must not claim otherwise.
    """
    if not isinstance(recorded, Mapping) or not recorded.get("digest"):
        return PlanDrift(changed=True, comparable=False)
    if recorded.get("digest") == current.get("digest"):
        return PlanDrift(changed=False)

    recorded_tasks = recorded.get("tasks")
    current_tasks = current.get("tasks")
    recorded_tasks = recorded_tasks if isinstance(recorded_tasks, Mapping) else {}
    current_tasks = current_tasks if isinstance(current_tasks, Mapping) else {}

    added = tuple(sorted(set(current_tasks) - set(recorded_tasks)))
    removed = tuple(sorted(set(recorded_tasks) - set(current_tasks)))

    changed: list[tuple[str, tuple[str, ...]]] = []
    for task_id in sorted(set(recorded_tasks) & set(current_tasks)):
        before = recorded_tasks.get(task_id)
        after = current_tasks.get(task_id)
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            continue
        fields = tuple(
            sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        )
        if fields:
            changed.append((task_id, fields))

    recorded_order = list(recorded.get("task_order") or [])
    current_order = list(current.get("task_order") or [])
    return PlanDrift(
        changed=True,
        goal_changed=recorded.get("project_goal") != current.get("project_goal"),
        summary_changed=recorded.get("summary") != current.get("summary"),
        requirements_changed=recorded.get("requirements") != current.get("requirements"),
        order_changed=recorded_order != current_order,
        tasks_added=added,
        tasks_removed=removed,
        tasks_changed=tuple(changed),
    )


__all__ = [
    "PLAN_FINGERPRINT_KEY",
    "PLAN_FINGERPRINT_SCHEMA_VERSION",
    "RESUMABLE_RUN_STATUSES",
    "RUN_OWNER_KEY",
    "RUN_STATE_SCHEMA_VERSION",
    "RUN_STATUSES",
    "RUN_STATUS_APPROVED",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_DRAFT",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_INTERRUPTED",
    "RUN_STATUS_RUNNING",
    "STATUS_HISTORY_KEY",
    "STATUS_KEY",
    "TERMINAL_RUN_STATUSES",
    "PlanDrift",
    "apply_run_status",
    "build_run_owner",
    "compare_plan_fingerprints",
    "describe_run_status",
    "normalize_run_status",
    "plan_fingerprint",
    "pointer_status",
    "reconcile_status_after_crash",
    "status_is_resumable",
    "status_is_terminal",
]
