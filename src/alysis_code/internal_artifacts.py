"""Internal runtime artifacts and how they are kept out of user-facing output.

When a turn runs out of deadline or step budget, the runtime writes a local
"the turn stopped" report from whatever it can reconstruct of the transcript --
files read, commands run, what is still missing. For a top-level run that
report *is* the honest answer. For a **subagent** it is half-finished internal
state: the parent asked for a deliverable and got the child's runtime diary
instead, which then flowed verbatim into the parent's final summary.

The rule this module encodes: a locally generated stop report is an *internal
artifact*, and internal artifacts never become user-facing output. The
enforcement is a marker, set where the artifact is produced and honoured where
output is assembled -- never a search for characteristic phrases in the text.
Pattern-matching "Remaining work:" would break the moment the wording, the
language, or the model changed; a marker cannot silently stop matching.

Every function here is pure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# Key stamped on a transcript message whose content is an internal artifact.
# Harness-only, like PROVIDER_METADATA_KEY: it is stripped before the provider
# call and is not part of the wire format.
INTERNAL_ARTIFACT_MESSAGE_KEY = "alysis_internal_artifact"

# ``final_text_source`` reported by the subagent boundary when the child's last
# answer was a locally generated stop report rather than a real final report.
INTERNAL_FALLBACK_SOURCE = "internal_fallback"

SUBAGENT_INCOMPLETE_ERROR_CODE = "subagent_incomplete"
SUBAGENT_PARTIAL_REPORT_MAX_CHARS = 4096


class ArtifactVisibility(StrEnum):
    USER_FACING = "user_facing"
    INTERNAL = "internal"


def mark_message_internal(message: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """Stamp ``message`` as carrying an internal artifact. Returns it."""
    message[INTERNAL_ARTIFACT_MESSAGE_KEY] = str(kind or "internal")
    return message


def message_is_internal(message: Any) -> bool:
    return isinstance(message, Mapping) and bool(message.get(INTERNAL_ARTIFACT_MESSAGE_KEY))


def summary_input_messages(messages: Iterable[Any]) -> list[Any]:
    """Transcript projection for building a user-facing summary.

    Drops messages marked as internal artifacts. This is what makes the
    exclusion structural: the summary builder cannot see an internal artifact
    because it is never handed one, regardless of what the artifact says.
    """
    return [message for message in messages if not message_is_internal(message)]


@dataclass(frozen=True)
class SubagentIncompleteStatus:
    """Structured result handed to the parent when a subagent did not finish.

    Deliberately not prose: the parent has to *decide* (retry, absorb the work,
    or report an honest failure), and a decision needs fields, not a narrative
    that reads like a deliverable.
    """

    subagent: str
    reason: str
    steps_used: int
    deadline_s: float | None
    resolved_step_ceiling: int | None = None
    deadline_remaining_s: float | None = None
    report_artifact: str = ""
    subagent_session_id: str = ""
    elapsed_ms: int = 0
    run_id: str = ""
    retained_worktree_run_id: str = ""
    resume_affordance: str = ""
    partial_report_excerpt: str = ""
    partial_report_truncated: bool = False
    partial_report_safety: dict[str, Any] | None = None

    @property
    def message(self) -> str:
        message = f"Subagent '{self.subagent}' stopped before finishing ({self.reason}). "
        if self.partial_report_excerpt:
            message += (
                "A screened partial excerpt is included for continuity; it is not a "
                "completed finding. Retry with a narrower task, do the work directly, "
                "or report the blocker."
            )
        else:
            message += (
                "Its partial-state report is internal and was not returned. Retry with a "
                "narrower task, do the work directly, or report the blocker."
            )
        if self.resume_affordance:
            message += f"\n{self.resume_affordance}"
        return message

    def diagnostics_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stop_reason": self.reason,
            "steps_used": self.steps_used,
            "resolved_step_ceiling": self.resolved_step_ceiling,
            "deadline_remaining_s": self.deadline_remaining_s,
        }
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.retained_worktree_run_id:
            payload["retained_worktree_run_id"] = self.retained_worktree_run_id
        if self.resume_affordance:
            payload["resume_affordance"] = self.resume_affordance
        return payload

    def telemetry_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subagent": self.subagent,
            "reason": self.reason,
            "deadline_s": self.deadline_s,
            "artifact_visibility": ArtifactVisibility.INTERNAL.value,
            **self.diagnostics_payload(),
        }
        if self.subagent_session_id:
            payload["subagent_session_id"] = self.subagent_session_id
        if self.report_artifact:
            payload["report_artifact"] = self.report_artifact
        return payload

    def tool_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error": self.message,
            "subagent": self.subagent,
            "status": "incomplete",
            "failure_category": "incomplete",
            "error_code": SUBAGENT_INCOMPLETE_ERROR_CODE,
            "incomplete_reason": self.reason,
            "deadline_s": self.deadline_s,
            "elapsed_ms": self.elapsed_ms,
            **self.diagnostics_payload(),
        }
        if self.subagent_session_id:
            result["subagent_session_id"] = self.subagent_session_id
        if self.report_artifact:
            result["report_artifact"] = self.report_artifact
            result["report_artifact_reader"] = "session_artifact_read"
        if self.partial_report_excerpt:
            result["partial_report"] = {
                "status": "partial",
                "excerpt": self.partial_report_excerpt,
                "truncated": self.partial_report_truncated,
                "max_chars": SUBAGENT_PARTIAL_REPORT_MAX_CHARS,
                "safety": dict(self.partial_report_safety or {}),
            }
        return result


def subagent_report_is_internal(final_text_source: str) -> bool:
    """True when the child's last answer was a locally generated stop report."""
    return str(final_text_source or "").strip() == INTERNAL_FALLBACK_SOURCE


def resolve_incomplete_reason(
    *,
    termination_kind: str,
    deadline_exhausted: bool,
) -> str:
    """Name why the subagent stopped, preferring the recorded termination kind."""
    kind = str(termination_kind or "").strip()
    if kind:
        return kind
    return "deadline_exhausted" if deadline_exhausted else "step_budget_exhausted"
