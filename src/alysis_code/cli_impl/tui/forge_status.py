"""Single source of truth for Forge task-status → visual bucket/glyph mapping.

Historically the same four status sets (done / failure / obsolete / running)
were copy-pasted into :mod:`...tui.app`, :mod:`...tui.surface`,
:mod:`...commands.chat_tui_panels`, and :mod:`...commands.cli_common`, and had
started to drift. This module holds them ONCE so the live Forge table, the
``/show`` plan panel, and the end-of-run summary can never disagree about
whether a task is done, failed, or still running.

Kept deliberately pure (stdlib only, no package imports) so it is a leaf that
any layer — the transcript model, the alt-screen renderer, or the standalone
panel-spec builders — can import without pulling in the agent runtime or
prompt_toolkit, and so it stays unit-testable in isolation.

Matching is on the RAW, whitespace-stripped, lower-cased status string (the
same semantics the individual sites used before consolidation). Callers that
want canonicalisation (e.g. the plan panel) canonicalise first and pass the
canonical value in — canonical statuses are already lower-case, so membership
still resolves.
"""

from __future__ import annotations

# Terminal-success states: the task is finished and satisfied.
# ``completed_unverified`` (see ``...verification_repair``, spelled literally here to
# keep this module import-free) means the work landed but no authoritative command
# existed to check it. That is a completion, not a failure — a missing test runner
# must never render as a red ✗ against work that is on disk.
FORGE_DONE_STATES: frozenset[str] = frozenset({"done", "already_satisfied", "completed_unverified"})

# Terminal-failure states: the task ended blocked/rejected/interrupted.
FORGE_FAILURE_STATES: frozenset[str] = frozenset(
    {
        "failed",
        "verify_failed",
        "candidate_rejected",
        "changes_requested",
        "merge_conflict",
        "blocked_integration",
        "blocked",
        "interrupted",
        "cancelled",
    }
)

# Non-executable / superseded states: excluded from done AND remaining counts.
FORGE_OBSOLETE_STATES: frozenset[str] = frozenset({"superseded", "invalidated"})

# Actively-executing states written to plan.json while a worker holds the task.
FORGE_RUNNING_STATES: frozenset[str] = frozenset({"in_progress", "running", "executing", "active"})

# Any state in which the task is no longer running (used to stop spinning a row
# and prune it from the live "active workers" set).
FORGE_TERMINAL_STATES: frozenset[str] = (
    FORGE_DONE_STATES | FORGE_FAILURE_STATES | FORGE_OBSOLETE_STATES
)


def forge_status_bucket(status: str) -> str:
    """Map a task status to a visual bucket.

    Returns one of ``"done" | "failed" | "obsolete" | "running" | "remaining"``.
    ``"remaining"`` covers planned/todo/unknown states (everything not otherwise
    classified).
    """
    s = str(status or "").strip().lower()
    if s in FORGE_DONE_STATES:
        return "done"
    if s in FORGE_FAILURE_STATES:
        return "failed"
    if s in FORGE_OBSOLETE_STATES:
        return "obsolete"
    if s in FORGE_RUNNING_STATES:
        return "running"
    return "remaining"


def forge_status_is_terminal(status: str) -> bool:
    """True when the task is no longer running (done, failed, or obsolete)."""
    return str(status or "").strip().lower() in FORGE_TERMINAL_STATES


def forge_status_glyph(status: str, *, active: bool = False, spinner: str = "") -> str:
    """Return the one-column glyph for a task row.

    ``✓`` done · ``✗`` failed · ``·`` obsolete · a spinner frame for a running
    (or explicitly-active) task · ``○`` for everything still to do. ``active``
    is the live-view flag for "the swarm is working this right now" — it spins a
    task whose on-disk status has not yet flipped to a running state, but a
    terminal (done/failed) status always wins so a finished row never spins.
    """
    bucket = forge_status_bucket(status)
    if active and bucket not in ("done", "failed", "obsolete"):
        return spinner or "◐"
    if bucket == "done":
        return "✓"
    if bucket == "failed":
        return "✗"
    if bucket == "obsolete":
        return "·"
    if bucket == "running":
        return spinner or "◐"
    return "○"


def forge_status_counts(statuses: object) -> tuple[int, int, int]:
    """Return ``(done, failed, remaining)`` for an iterable of status strings.

    Obsolete states are excluded from all three buckets (they are neither done
    nor outstanding) — matching ``cli_common._forge_task_status_counts``.
    """
    done = failed = remaining = 0
    for status in statuses or ():
        bucket = forge_status_bucket(str(status))
        if bucket == "done":
            done += 1
        elif bucket == "failed":
            failed += 1
        elif bucket == "obsolete":
            continue
        else:
            remaining += 1
    return done, failed, remaining


__all__ = [
    "FORGE_DONE_STATES",
    "FORGE_FAILURE_STATES",
    "FORGE_OBSOLETE_STATES",
    "FORGE_RUNNING_STATES",
    "FORGE_TERMINAL_STATES",
    "forge_status_bucket",
    "forge_status_is_terminal",
    "forge_status_glyph",
    "forge_status_counts",
]
