"""Validate-and-repair policy for planner payloads.

The planner is asked for one JSON object per turn. When the model returns
something the schema rejects, the honest first move is to tell the model exactly
what was wrong and ask again -- not to quietly reshape the payload host-side and
pretend the model produced it. This module holds the facts that bound that
retry loop, plus the vocabulary for reporting what happened when the loop ends.

It is pure: no LLM calls, no filesystem, no clock. Everything here is a function
of a plan dict, a payload dict, or a config object.

* ``resolve_plan_repair_policy`` -- attempt caps and the kill-switch.
* ``repair_retry_instruction`` -- the escalating-strictness follow-up text. Each
  attempt restates the same schema demand in blunter terms and drops more of the
  model's latitude, because a model that ignored the polite version has already
  demonstrated the polite version does not work on it.
* ``host_repaired_field_paths`` -- which fields host-side repair had to change,
  so a salvaged payload is labelled rather than passed off as the model's own.
* ``execution_readiness_errors`` -- the R1-R5 acceptance rules rendered as
  re-prompt material, so an unexecutable plan is a retry trigger and not a
  surprise at ``forge exec`` time.
* ``ClarificationLoopTracker`` -- consecutive clarification rounds for one goal.
  Past the cap the caller forces a concrete draft instead of asking forever.
* ``assess_plan_status`` -- draft vs execution_ready, decided by the *same* rule
  the execution gate uses, so a plan that will be blocked later says so now.

Nothing here inspects prose or vocabulary: every decision is driven by payload
shape, plan structure, and the acceptance rules already in
:mod:`alysis_code.plan_validation`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .branding import env_get
from .plan_validation import PlanAcceptanceIssue, find_plan_acceptance_issues

# ---------------------------------------------------------------------------
# Policy (kill-switch mirrors the empty-response-stall / evidence-v2 idiom)
# ---------------------------------------------------------------------------

DEFAULT_MAX_PAYLOAD_ATTEMPTS = 3
DEFAULT_MAX_CLARIFICATION_ROUNDS = 2

# Hard ceilings. A misconfigured value must not turn one planner turn into an
# unbounded spend; the loop is a repair mechanism, not a search strategy.
MAX_PAYLOAD_ATTEMPTS_CEILING = 6
MAX_CLARIFICATION_ROUNDS_CEILING = 6

PLAN_STATUS_DRAFT = "draft"
PLAN_STATUS_EXECUTION_READY = "execution_ready"

PLAN_STATUS_KEY = "plan_status"
PLAN_STATUS_DETAIL_KEY = "plan_status_detail"
PLAN_REPAIR_KEY = "plan_repair"

# Terminal states of one planner turn. Exactly one of these is always reached.
TERMINAL_VALIDATED = "validated"
TERMINAL_HOST_REPAIRED = "host_repaired"
TERMINAL_FORCED_DRAFT = "forced_draft"
TERMINAL_FAILED = "failed"

_TRUTHY = {"on", "1", "true", "yes", "enabled"}
_FALSEY = {"off", "0", "false", "no", "disabled"}


@dataclass(frozen=True)
class PlanRepairPolicy:
    """Bounds for one planner turn's validate-and-repair loop."""

    enabled: bool = True
    max_payload_attempts: int = DEFAULT_MAX_PAYLOAD_ATTEMPTS
    max_clarification_rounds: int = DEFAULT_MAX_CLARIFICATION_ROUNDS

    @property
    def max_repair_retries(self) -> int:
        """Follow-up requests allowed after the first response."""
        return max(self.max_payload_attempts - 1, 0)


def plan_repair_enabled(cfg: Any | None) -> bool:
    """Kill-switch for the general validate-and-repair loop.

    ``ALYSIS_PLAN_REPAIR`` wins over config; default is on. When off the
    policy collapses to the legacy shape: one schema retry, host-side repair
    applied eagerly, no execution-readiness retry, no clarification cap.
    """
    flag = _env_flag("ALYSIS_PLAN_REPAIR")
    if flag is not None:
        return flag
    return bool(getattr(cfg, "plan_repair_enabled", True))


def resolve_plan_repair_policy(cfg: Any | None) -> PlanRepairPolicy:
    """Resolve attempt caps from env, then config, then the defaults."""
    enabled = plan_repair_enabled(cfg)
    attempts = _bounded_int(
        _env_int("ALYSIS_PLAN_REPAIR_ATTEMPTS"),
        getattr(cfg, "plan_repair_max_attempts", None),
        fallback=DEFAULT_MAX_PAYLOAD_ATTEMPTS,
        ceiling=MAX_PAYLOAD_ATTEMPTS_CEILING,
    )
    clarifications = _bounded_int(
        _env_int("ALYSIS_PLAN_REPAIR_CLARIFICATION_ROUNDS"),
        getattr(cfg, "plan_repair_max_clarification_rounds", None),
        fallback=DEFAULT_MAX_CLARIFICATION_ROUNDS,
        ceiling=MAX_CLARIFICATION_ROUNDS_CEILING,
    )
    if not enabled:
        # Legacy shape: the single retry that predates this module.
        return PlanRepairPolicy(
            enabled=False,
            max_payload_attempts=2,
            max_clarification_rounds=0,
        )
    return PlanRepairPolicy(
        enabled=True,
        max_payload_attempts=attempts,
        max_clarification_rounds=clarifications,
    )


def _env_flag(name: str) -> bool | None:
    raw = env_get(name)
    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    if normalized in _FALSEY:
        return False
    if normalized in _TRUTHY:
        return True
    return None


def _env_int(name: str) -> int | None:
    raw = env_get(name)
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _bounded_int(*candidates: Any, fallback: int, ceiling: int) -> int:
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            parsed = int(candidate)
        except (TypeError, ValueError):
            continue
        if parsed < 1:
            continue
        return min(parsed, ceiling)
    return min(max(fallback, 1), ceiling)


# ---------------------------------------------------------------------------
# Escalating-strictness retry text
# ---------------------------------------------------------------------------

# Rungs for the intermediate follow-ups, in order. The last rung repeats when the
# attempt cap is raised past the ladder, so a bigger budget never runs out of text.
_STRICTNESS_LADDER: tuple[str, ...] = (
    (
        "Return the corrected payload as ONE JSON object. Keep every field that was "
        "already correct; change only what the validation errors name."
    ),
    (
        "STRICT MODE. The previous correction also failed validation. Output MUST begin "
        "with '{' and end with '}'. No markdown fence, no prose before or after, no "
        "commentary field, no keys outside the documented schema. Every array field must "
        "be a JSON array of strings even when it holds a single value. If you are unsure "
        "about an optional field, omit the key entirely rather than guessing its shape."
    ),
)

# Reserved for the last allowed attempt, whatever the cap is: it is the only rung
# that tells the model what happens when it fails again.
_FINAL_STRICTNESS = (
    "FINAL ATTEMPT. Emit the smallest payload that validates. Drop every optional key "
    "you are not certain about, keep only 'assistant_message' plus the plan_update "
    "fields you can state exactly, and re-check each value's type against the schema "
    "before answering. After this attempt the host repairs the payload itself and the "
    "resulting plan is recorded as host-repaired rather than model-authored."
)


def repair_strictness_instruction(*, attempt: int, max_attempts: int) -> str:
    """Instruction text for follow-up ``attempt`` (2-based: the first retry is 2)."""
    if int(attempt) >= int(max_attempts):
        return _FINAL_STRICTNESS
    rung = min(max(int(attempt) - 2, 0), len(_STRICTNESS_LADDER) - 1)
    return _STRICTNESS_LADDER[rung]


def repair_retry_instruction(
    *,
    attempt: int,
    max_attempts: int,
    validation_errors: Sequence[str],
    previous_response: str | None = None,
    kind: str = "schema",
) -> str:
    """Follow-up block naming the exact errors, the attempt number, and the strictness rung.

    ``previous_response`` is optional so a caller that already quotes the model's
    last output does not quote it twice in one prompt.
    """
    errors = [str(item).strip() for item in validation_errors if str(item).strip()]
    if kind == "execution_readiness":
        headline = (
            "Your previous planner output parsed, but the plan it produces cannot be "
            "executed. Every listed rule must be satisfied or the plan is rejected at "
            "execution time."
        )
        error_label = "Execution-readiness errors"
    else:
        headline = "Your previous planner output did not match the required schema."
        error_label = "Validation errors"
    rendered_errors = "\n".join(f"- {item}" for item in errors[:10]) or "- (unspecified)"
    if len(errors) > 10:
        rendered_errors += f"\n- (+{len(errors) - 10} more)"
    previous_block = (
        f"Previous response:\n{previous_response}\n\n" if previous_response is not None else ""
    )
    return (
        f"{headline}\n"
        f"Correction attempt {attempt} of {max_attempts}.\n"
        f"{error_label}:\n{rendered_errors}\n\n"
        f"{previous_block}"
        f"{repair_strictness_instruction(attempt=attempt, max_attempts=max_attempts)}\n"
        "Return the full corrected payload, not a diff or a patch. Preserve the latest "
        "user intent, target roots, and decoy/forbidden constraints from the prompt."
    )


# ---------------------------------------------------------------------------
# Host-side repair accounting
# ---------------------------------------------------------------------------

# A payload with thousands of leaves is already pathological; the cap keeps one
# bad response from writing an unbounded field list into the plan.
_MAX_REPAIRED_FIELD_PATHS = 60


def host_repaired_field_paths(*, raw: Any, repaired: Any) -> list[str]:
    """Dotted paths where host-side repair changed the model's payload.

    Both sides are pre-validation, so shared normalisation does not show up here:
    what remains is exactly what the repairer had to touch.
    """
    paths: list[str] = []
    _collect_diff_paths(raw, repaired, prefix="", out=paths)
    deduped = list(dict.fromkeys(paths))
    return sorted(deduped[:_MAX_REPAIRED_FIELD_PATHS])


def _collect_diff_paths(left: Any, right: Any, *, prefix: str, out: list[str]) -> None:
    if len(out) >= _MAX_REPAIRED_FIELD_PATHS:
        return
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in sorted({*left.keys(), *right.keys()}, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                out.append(child)
                continue
            _collect_diff_paths(left[key], right[key], prefix=child, out=out)
        return
    if _is_plain_sequence(left) and _is_plain_sequence(right):
        if len(left) != len(right):
            out.append(prefix or "(root)")
            return
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
            _collect_diff_paths(
                left_item,
                right_item,
                prefix=f"{prefix}[{index}]",
                out=out,
            )
        return
    if left != right:
        out.append(prefix or "(root)")


def _is_plain_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


# ---------------------------------------------------------------------------
# Execution readiness as a retry trigger
# ---------------------------------------------------------------------------


def format_acceptance_issue(issue: PlanAcceptanceIssue) -> str:
    parts = [str(issue.rule_id)]
    if issue.task_id:
        parts.append(f"task={issue.task_id}")
    parts.append(f"observed={issue.observed}")
    if issue.detail:
        parts.append(f"detail={issue.detail}")
    return " ".join(parts)


def execution_readiness_errors(plan: Mapping[str, Any]) -> list[str]:
    """R1-R5 acceptance failures for ``plan``, rendered for a re-prompt."""
    try:
        issues = find_plan_acceptance_issues(dict(plan))
    except Exception:  # noqa: BLE001 - readiness reporting must not break the turn
        return []
    return [format_acceptance_issue(issue) for issue in issues]


def plan_update_proposes_task_work(plan_update: Any) -> bool:
    """True when the payload asks for task work that execution would have to run.

    A clarifying turn -- questions, a goal edit, requirement notes -- proposes no
    task work, so holding it to the execution-acceptance rules would burn the
    retry budget on a payload that was never meant to be executable.
    """
    if not isinstance(plan_update, Mapping):
        return False
    for key in ("tasks_add", "tasks_update"):
        entries = plan_update.get(key)
        if isinstance(entries, Sequence) and not isinstance(entries, str | bytes) and len(entries):
            return True
    return False


# ---------------------------------------------------------------------------
# Clarification loop cap
# ---------------------------------------------------------------------------


def clarification_goal_key(*, plan: Mapping[str, Any] | None, user_text: str) -> str:
    """Stable key for "the same goal", so a new goal restarts the count.

    The plan's own goal is the anchor when it has one; before that exists the
    first message of the streak is all there is to key on.
    """
    goal = str((plan or {}).get("project_goal") or "").strip()
    if goal:
        return " ".join(goal.split()).casefold()[:200]
    return " ".join(str(user_text or "").split()).casefold()[:200]


@dataclass
class ClarificationLoopTracker:
    """Consecutive clarification-only planner turns for one goal."""

    max_rounds: int = DEFAULT_MAX_CLARIFICATION_ROUNDS
    goal_key: str = ""
    rounds: int = 0

    def rounds_for(self, goal_key: str) -> int:
        """Rounds already recorded for ``goal_key`` (0 when the goal changed)."""
        return self.rounds if goal_key == self.goal_key else 0

    def record(self, *, goal_key: str, awaiting_clarification: bool) -> int:
        """Record one planner turn's outcome and return the running round count."""
        if goal_key != self.goal_key:
            self.goal_key = goal_key
            self.rounds = 0
        if not awaiting_clarification:
            self.rounds = 0
            return 0
        self.rounds += 1
        return self.rounds

    def cap_reached(self, goal_key: str) -> bool:
        if self.max_rounds <= 0:
            return False
        return self.rounds_for(goal_key) >= self.max_rounds

    def reset(self) -> None:
        self.goal_key = ""
        self.rounds = 0


def payload_awaits_clarification(validated: Mapping[str, Any] | None) -> bool:
    """True when the payload asks the user something and proposes no plan work."""
    if not isinstance(validated, Mapping):
        return False
    if validated.get("plan_update"):
        return False
    questions = validated.get("questions")
    if not isinstance(questions, Sequence) or isinstance(questions, str | bytes):
        return False
    return any(str(item).strip() for item in questions)


# ---------------------------------------------------------------------------
# Turn report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerRepairReport:
    """What the validate-and-repair loop did during one planner turn."""

    terminal_state: str = TERMINAL_VALIDATED
    attempts: int = 1
    max_attempts: int = DEFAULT_MAX_PAYLOAD_ATTEMPTS
    schema_retries: int = 0
    readiness_retries: int = 0
    host_repaired: bool = False
    host_repaired_fields: list[str] = field(default_factory=list)
    execution_readiness_errors: list[str] = field(default_factory=list)
    clarification_rounds: int = 0
    forced_draft: bool = False
    validation_errors: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True when the turn did not produce what a clean planner turn would.

        A host-repaired payload, a forced draft, and a payload that never became
        execution-ready all qualify: each is worth keeping in the plan's history,
        because each explains a plan that is not what the planner was asked for.
        """
        return bool(self.host_repaired or self.forced_draft or self.execution_readiness_errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal_state": self.terminal_state,
            "attempts": int(self.attempts),
            "max_attempts": int(self.max_attempts),
            "schema_retries": int(self.schema_retries),
            "readiness_retries": int(self.readiness_retries),
            "host_repaired": bool(self.host_repaired),
            "host_repaired_fields": list(self.host_repaired_fields),
            "execution_readiness_errors": list(self.execution_readiness_errors),
            "clarification_rounds": int(self.clarification_rounds),
            "forced_draft": bool(self.forced_draft),
            "validation_errors": list(self.validation_errors),
        }


# ---------------------------------------------------------------------------
# Plan status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStatusAssessment:
    """Whether a saved plan can be executed, and why not when it cannot."""

    status: str
    blocking_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def execution_ready(self) -> bool:
        return self.status == PLAN_STATUS_EXECUTION_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "blocking_reasons": list(self.blocking_reasons),
            "warnings": list(self.warnings),
        }


def assess_plan_status(
    plan: Mapping[str, Any],
    *,
    validation_warnings: Sequence[str] | None = None,
) -> PlanStatusAssessment:
    """Classify a plan as draft or execution_ready.

    The rule is the execution gate's own rule, so the two can never disagree: a
    plan is ``execution_ready`` exactly when ``forge exec`` would not reject it.
    ``validate_plan`` warnings (a missing ``acceptance_criteria``, say) are
    recorded but do not by themselves make a plan a draft -- they are advisory,
    and calling them blocking here would contradict the gate.
    """
    blocking = execution_readiness_errors(plan)
    warnings = [str(item).strip() for item in (validation_warnings or []) if str(item).strip()]
    status = PLAN_STATUS_DRAFT if blocking else PLAN_STATUS_EXECUTION_READY
    return PlanStatusAssessment(
        status=status,
        blocking_reasons=blocking,
        warnings=warnings,
    )


def apply_plan_status(
    plan: dict[str, Any],
    *,
    validation_warnings: Sequence[str] | None = None,
    assessment: PlanStatusAssessment | None = None,
) -> PlanStatusAssessment:
    """Write the draft/execution_ready status onto ``plan`` and return it."""
    resolved = (
        assessment
        if assessment is not None
        else assess_plan_status(plan, validation_warnings=validation_warnings)
    )
    plan[PLAN_STATUS_KEY] = resolved.status
    plan[PLAN_STATUS_DETAIL_KEY] = {
        "blocking_reasons": list(resolved.blocking_reasons),
        "warnings": list(resolved.warnings),
    }
    return resolved


def resolved_plan_status(plan: Mapping[str, Any]) -> PlanStatusAssessment:
    """The plan's status as recorded, or assessed live when it was never recorded.

    Status and reasons always come from the same source: a plan saved before this
    field existed must not report a status from one place and empty reasons from
    another.
    """
    recorded = str(plan.get(PLAN_STATUS_KEY) or "").strip()
    detail = plan.get(PLAN_STATUS_DETAIL_KEY)
    if recorded in {PLAN_STATUS_DRAFT, PLAN_STATUS_EXECUTION_READY} and isinstance(detail, Mapping):
        return PlanStatusAssessment(
            status=recorded,
            blocking_reasons=[str(item) for item in detail.get("blocking_reasons") or []],
            warnings=[str(item) for item in detail.get("warnings") or []],
        )
    return assess_plan_status(plan)


def plan_status(plan: Mapping[str, Any]) -> str:
    """Recorded status, or the live assessment for a plan saved before this existed."""
    return resolved_plan_status(plan).status


def plan_status_detail(plan: Mapping[str, Any]) -> dict[str, Any]:
    assessment = resolved_plan_status(plan)
    return {
        "blocking_reasons": list(assessment.blocking_reasons),
        "warnings": list(assessment.warnings),
    }


# ---------------------------------------------------------------------------
# Repair metadata on the plan
# ---------------------------------------------------------------------------

# Keep the on-plan history bounded: the most recent turns are what a reader
# needs, and plan.json is re-serialised on every save.
_MAX_RECORDED_REPAIRS = 20


def record_plan_repair(plan: dict[str, Any], report: PlannerRepairReport) -> dict[str, Any]:
    """Append one turn's repair report to the plan's repair metadata.

    Only degraded turns are recorded. A turn the model got right on its own adds
    no history, so the presence of this block always means something was salvaged.
    """
    metadata = plan.get(PLAN_REPAIR_KEY)
    if not isinstance(metadata, dict):
        metadata = {}
    entries = metadata.get("entries")
    if not isinstance(entries, list):
        entries = []

    if report.degraded:
        entries.append(report.to_dict())
        entries = entries[-_MAX_RECORDED_REPAIRS:]

    host_fields: list[str] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            host_fields.extend(str(item) for item in entry.get("host_repaired_fields") or [])

    metadata["entries"] = entries
    metadata["host_repaired_fields"] = sorted(dict.fromkeys(host_fields))
    metadata["host_repaired"] = any(
        bool(entry.get("host_repaired")) for entry in entries if isinstance(entry, Mapping)
    )
    metadata["forced_draft"] = any(
        bool(entry.get("forced_draft")) for entry in entries if isinstance(entry, Mapping)
    )
    if entries:
        plan[PLAN_REPAIR_KEY] = metadata
    return metadata


def plan_repair_metadata(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Repair metadata for ``plan``, normalised for reporting surfaces."""
    metadata = plan.get(PLAN_REPAIR_KEY)
    if not isinstance(metadata, Mapping):
        return {
            "host_repaired": False,
            "host_repaired_fields": [],
            "forced_draft": False,
            "entries": [],
        }
    entries = [dict(entry) for entry in metadata.get("entries") or [] if isinstance(entry, Mapping)]
    return {
        "host_repaired": bool(metadata.get("host_repaired")),
        "host_repaired_fields": [str(item) for item in metadata.get("host_repaired_fields") or []],
        "forced_draft": bool(metadata.get("forced_draft")),
        "entries": entries,
    }


def plan_repair_event_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The status + repair fields every ``plan_saved``/``plan_invalid`` event carries."""
    metadata = plan_repair_metadata(plan)
    assessment = resolved_plan_status(plan)
    return {
        "plan_status": assessment.status,
        "plan_status_blocking_reasons": list(assessment.blocking_reasons),
        "host_repaired": metadata["host_repaired"],
        "host_repaired_fields": metadata["host_repaired_fields"],
        "forced_draft": metadata["forced_draft"],
    }


__all__ = [
    "DEFAULT_MAX_CLARIFICATION_ROUNDS",
    "DEFAULT_MAX_PAYLOAD_ATTEMPTS",
    "PLAN_REPAIR_KEY",
    "PLAN_STATUS_DETAIL_KEY",
    "PLAN_STATUS_DRAFT",
    "PLAN_STATUS_EXECUTION_READY",
    "PLAN_STATUS_KEY",
    "TERMINAL_FAILED",
    "TERMINAL_FORCED_DRAFT",
    "TERMINAL_HOST_REPAIRED",
    "TERMINAL_VALIDATED",
    "ClarificationLoopTracker",
    "PlanRepairPolicy",
    "PlanStatusAssessment",
    "PlannerRepairReport",
    "apply_plan_status",
    "assess_plan_status",
    "clarification_goal_key",
    "execution_readiness_errors",
    "format_acceptance_issue",
    "host_repaired_field_paths",
    "payload_awaits_clarification",
    "plan_repair_enabled",
    "plan_repair_event_payload",
    "plan_repair_metadata",
    "plan_status",
    "plan_status_detail",
    "plan_update_proposes_task_work",
    "record_plan_repair",
    "resolved_plan_status",
    "repair_retry_instruction",
    "repair_strictness_instruction",
    "resolve_plan_repair_policy",
]
