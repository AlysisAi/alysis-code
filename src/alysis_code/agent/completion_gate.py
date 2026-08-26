from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CompletionGateDecisionKind(StrEnum):
    ALLOW_FINAL = "ALLOW_FINAL"
    NUDGE_ONCE = "NUDGE_ONCE"


class CompletionGateActionKind(StrEnum):
    NONE = "none"
    IMPLEMENT_MATERIAL_WORK = "implement_material_work"
    RUN_VERIFICATION = "run_verification"
    REPAIR_VERIFICATION_FAILURE = "repair_verification_failure"
    SATISFY_ACCEPTANCE = "satisfy_acceptance"


NON_FINAL_PROGRESS_STAGE = "non_final_progress"
NON_FINAL_PROGRESS_PROBLEM = "non_final_progress"


_ABS_TEMP_PATH_RE = re.compile(
    r"(?:/private)?/(?:tmp|var/folders)/[^\s:;,\]\)]+|"
    r"[A-Za-z]:\\[^\s:;,\]\)]*\\(?:Temp|tmp)\\[^\s:;,\]\)]+"
)
_ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_CLOCK_TIME_RE = re.compile(r"\b[0-2]?\d:[0-5]\d(?::[0-5]\d(?:\.\d+)?)?\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds|msec)\b", re.I)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)
_HEX_ADDRESS_RE = re.compile(r"\b0x[0-9a-f]{6,}\b", re.I)
_LONG_NUMBER_RE = re.compile(r"\b\d{5,}\b")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().split())


def normalize_completion_gate_failure_signature(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    clean = _ABS_TEMP_PATH_RE.sub("<temp-path>", clean)
    clean = _ISO_TIMESTAMP_RE.sub("<timestamp>", clean)
    clean = _CLOCK_TIME_RE.sub("<time>", clean)
    clean = _DURATION_RE.sub("<duration>", clean)
    clean = _UUID_RE.sub("<uuid>", clean)
    clean = _HEX_ADDRESS_RE.sub("<hex>", clean)
    clean = _LONG_NUMBER_RE.sub("<number>", clean)
    clean = _WHITESPACE_RE.sub(" ", clean)
    return clean.strip()


@dataclass(frozen=True)
class CompletionGateEvidenceSnapshot:
    stage: str
    problems: tuple[str, ...] = tuple()
    material_edit_count: int = 0
    material_edit_tools: tuple[str, ...] = tuple()
    touched_repo_paths: tuple[str, ...] = tuple()
    verification_relevant_edit_generation: int = 0
    last_successful_verification_generation: int | None = None
    expected_verification_commands: tuple[str, ...] = tuple()
    covered_verification_commands: tuple[str, ...] = tuple()
    missing_verification_commands: tuple[str, ...] = tuple()
    failed_verification_signatures: tuple[str, ...] = tuple()
    verification_coverage_stale: bool = False
    last_verification_passed: bool | None = None
    last_verification_failure_category: str = ""
    accepted_blocker: bool = False
    blocked_response: bool = False
    blocked_response_allows_completion: bool = False
    verification_expected: bool = False
    final_text_present: bool = False
    repo_tool_activity_observed: bool = False
    acceptance_status_counts: dict[str, int] = field(default_factory=dict)
    acceptance_problems: tuple[str, ...] = tuple()
    acceptance_failure_signatures: tuple[str, ...] = tuple()

    def payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "material_edit_count": self.material_edit_count,
            "material_edit_tools": list(self.material_edit_tools),
            "touched_repo_paths": list(self.touched_repo_paths),
            "verification_relevant_edit_generation": self.verification_relevant_edit_generation,
            "last_successful_verification_generation": self.last_successful_verification_generation,
            "expected_verification_commands": list(self.expected_verification_commands),
            "covered_verification_commands": list(self.covered_verification_commands),
            "missing_verification_commands": list(self.missing_verification_commands),
            "failed_verification_signatures": list(self.failed_verification_signatures),
            "verification_coverage_stale": self.verification_coverage_stale,
            "last_verification_passed": self.last_verification_passed,
            "last_verification_failure_category": self.last_verification_failure_category,
            "accepted_blocker": self.accepted_blocker,
            "blocked_response": self.blocked_response,
            "blocked_response_allows_completion": self.blocked_response_allows_completion,
            "verification_expected": self.verification_expected,
            "final_text_present": self.final_text_present,
            "repo_tool_activity_observed": self.repo_tool_activity_observed,
            "acceptance_status_counts": dict(sorted(self.acceptance_status_counts.items())),
            "acceptance_problems": list(self.acceptance_problems),
            "acceptance_failure_signatures": list(self.acceptance_failure_signatures),
        }


@dataclass
class CompletionGateControllerState:
    checklist_sent: bool = False
    total_rejected_finalizations: int = 0
    last_decision_kind: str = ""
    last_reason: str = ""
    last_stage: str = ""
    last_problems: tuple[str, ...] = tuple()
    last_snapshot_payload: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "checklist_sent": self.checklist_sent,
            "total_rejected_finalizations": self.total_rejected_finalizations,
            "last_decision_kind": self.last_decision_kind,
            "last_reason": self.last_reason,
            "last_stage": self.last_stage,
            "last_problems": list(self.last_problems),
            "last_snapshot_payload": dict(self.last_snapshot_payload),
        }


@dataclass(frozen=True)
class CompletionGateDecision:
    kind: CompletionGateDecisionKind
    stage: str
    problems: tuple[str, ...]
    reason: str
    recommended_action: str = CompletionGateActionKind.NONE.value
    preferred_tool_names: tuple[str, ...] = tuple()
    snapshot_payload: dict[str, Any] = field(default_factory=dict)
    checklist_already_sent: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "decision": self.kind.value,
            "stage": self.stage,
            "problems": list(self.problems),
            "reason": self.reason,
            "recommended_action": self.recommended_action,
            "preferred_tool_names": list(self.preferred_tool_names),
            "snapshot_payload": dict(self.snapshot_payload),
            "checklist_already_sent": self.checklist_already_sent,
        }


def completion_gate_decision_payload(decision: CompletionGateDecision) -> dict[str, Any]:
    return decision.as_payload()


def build_completion_gate_snapshot(
    *,
    stage: str,
    problems: list[str] | tuple[str, ...],
    material_edit_count: int,
    material_edit_tools: set[str] | list[str] | tuple[str, ...],
    touched_repo_paths: set[str] | list[str] | tuple[str, ...],
    verification_relevant_edit_generation: int,
    last_successful_verification_generation: int | None,
    expected_verification_commands: set[str] | list[str] | tuple[str, ...],
    covered_verification_commands: set[str] | list[str] | tuple[str, ...],
    missing_verification_commands: set[str] | list[str] | tuple[str, ...],
    failed_verification_command_snippets: dict[str, str],
    verification_coverage_stale: bool,
    last_verification_passed: bool | None,
    last_verification_failure_category: str = "",
    accepted_blocker: bool = False,
    blocked_response: bool = False,
    blocked_response_allows_completion: bool = False,
    verification_expected: bool = False,
    final_text: str = "",
    repo_tool_activity_observed: bool = False,
    acceptance_status_counts: dict[str, int] | None = None,
    acceptance_problems: list[str] | tuple[str, ...] = tuple(),
    acceptance_failure_summaries: list[str] | tuple[str, ...] = tuple(),
) -> CompletionGateEvidenceSnapshot:
    failed_signatures = []
    for command, snippet in sorted(failed_verification_command_snippets.items()):
        normalized_command = _normalize_command(command)
        normalized_snippet = normalize_completion_gate_failure_signature(snippet)
        if normalized_command or normalized_snippet:
            failed_signatures.append(f"{normalized_command}: {normalized_snippet}".strip(": "))
    return CompletionGateEvidenceSnapshot(
        stage=str(stage or "generic"),
        problems=tuple(str(item) for item in problems),
        material_edit_count=max(0, int(material_edit_count)),
        material_edit_tools=tuple(sorted(str(item) for item in material_edit_tools if str(item))),
        touched_repo_paths=tuple(sorted(str(item) for item in touched_repo_paths if str(item))),
        verification_relevant_edit_generation=max(0, int(verification_relevant_edit_generation)),
        last_successful_verification_generation=last_successful_verification_generation,
        expected_verification_commands=tuple(
            sorted(_normalize_command(item) for item in expected_verification_commands if str(item))
        ),
        covered_verification_commands=tuple(
            sorted(_normalize_command(item) for item in covered_verification_commands if str(item))
        ),
        missing_verification_commands=tuple(
            sorted(_normalize_command(item) for item in missing_verification_commands if str(item))
        ),
        failed_verification_signatures=tuple(sorted(failed_signatures)),
        verification_coverage_stale=bool(verification_coverage_stale),
        last_verification_passed=last_verification_passed,
        last_verification_failure_category=str(last_verification_failure_category or ""),
        accepted_blocker=bool(accepted_blocker),
        blocked_response=bool(blocked_response),
        blocked_response_allows_completion=bool(blocked_response_allows_completion),
        verification_expected=bool(verification_expected),
        final_text_present=bool(str(final_text or "").strip()),
        repo_tool_activity_observed=bool(repo_tool_activity_observed),
        acceptance_status_counts=dict(acceptance_status_counts or {}),
        acceptance_problems=tuple(sorted(str(item) for item in acceptance_problems if str(item))),
        acceptance_failure_signatures=tuple(
            sorted(
                normalize_completion_gate_failure_signature(str(item))
                for item in acceptance_failure_summaries
                if str(item).strip()
            )
        ),
    )


def _recommended_action_for_snapshot(
    snapshot: CompletionGateEvidenceSnapshot,
) -> str:
    problems = set(snapshot.problems)
    if "no_material_edits" in problems:
        return CompletionGateActionKind.IMPLEMENT_MATERIAL_WORK.value
    if "verification_failed" in problems:
        return CompletionGateActionKind.REPAIR_VERIFICATION_FAILURE.value
    if "verification_incomplete" in problems or "verification_not_attempted" in problems:
        return CompletionGateActionKind.RUN_VERIFICATION.value
    if (
        "acceptance_criteria_failed" in problems
        or "unexpected_scope_changes" in problems
        or "acceptance_criteria_unverified" in problems
        or "acceptance_evidence_insufficient" in problems
    ):
        return CompletionGateActionKind.SATISFY_ACCEPTANCE.value
    return CompletionGateActionKind.NONE.value


def decide_completion_gate(
    controller_state: CompletionGateControllerState,
    snapshot: CompletionGateEvidenceSnapshot,
    *,
    budget_exhausted: bool = False,
) -> CompletionGateDecision:
    _ = budget_exhausted
    snapshot_payload = snapshot.payload()
    recommended_action = _recommended_action_for_snapshot(snapshot)
    if not snapshot.problems:
        return CompletionGateDecision(
            kind=CompletionGateDecisionKind.ALLOW_FINAL,
            stage=snapshot.stage,
            problems=tuple(),
            reason="requirements_satisfied",
            recommended_action=CompletionGateActionKind.NONE.value,
            snapshot_payload=snapshot_payload,
        )
    if controller_state.checklist_sent:
        return CompletionGateDecision(
            kind=CompletionGateDecisionKind.ALLOW_FINAL,
            stage=snapshot.stage,
            problems=snapshot.problems,
            reason="advisory_checklist_already_sent",
            recommended_action=recommended_action,
            snapshot_payload=snapshot_payload,
            checklist_already_sent=True,
        )
    return CompletionGateDecision(
        kind=CompletionGateDecisionKind.NUDGE_ONCE,
        stage=snapshot.stage,
        problems=snapshot.problems,
        reason="advisory_checklist_needed",
        recommended_action=recommended_action,
        snapshot_payload=snapshot_payload,
    )


def record_completion_gate_decision(
    controller_state: CompletionGateControllerState,
    decision: CompletionGateDecision,
) -> None:
    controller_state.last_decision_kind = decision.kind.value
    controller_state.last_reason = decision.reason
    controller_state.last_stage = decision.stage
    controller_state.last_problems = decision.problems
    controller_state.last_snapshot_payload = dict(decision.snapshot_payload)
    if decision.kind == CompletionGateDecisionKind.NUDGE_ONCE:
        controller_state.checklist_sent = True
        controller_state.total_rejected_finalizations += 1
