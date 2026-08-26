from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .acceptance_contract import (
    AcceptanceContract,
    AcceptanceCriterion,
    AcceptanceCriterionEnforcement,
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
)


class CompletionCertificateStatus(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    CONTRADICTED = "CONTRADICTED"


_EVIDENCE_RANK = {
    "HOST_AUTHORITATIVE": 0,
    "USER_EXPLICIT": 1,
    "PREEXISTING_TASK_CHECKER": 2,
    "DIRECT_BLACK_BOX": 3,
    "PREEXISTING_REPO_NATIVE": 4,
    "SELF_AUTHORED": 5,
    "AD_HOC_OBSERVATION": 6,
}


@dataclass(frozen=True)
class CompletionCertificate:
    status: CompletionCertificateStatus
    problems: tuple[str, ...] = tuple()
    hard_criterion_ids: tuple[str, ...] = tuple()
    covered_hard_criterion_ids: tuple[str, ...] = tuple()
    failed_hard_criterion_ids: tuple[str, ...] = tuple()
    evidence_hierarchy: tuple[str, ...] = tuple()
    reason: str = ""
    # Baseline-first regression attribution (step 3): failing test ids mechanically
    # split against the pre-edit same-command baseline. pre_existing/agent_authored
    # do not block; regressions/unattributed do (see gate policy).
    regressions: tuple[str, ...] = tuple()
    unattributed_failures: tuple[str, ...] = tuple()
    pre_existing_failures: tuple[str, ...] = tuple()
    agent_authored_failures: tuple[str, ...] = tuple()
    # Turn-contract v2 (step 4): expectation ids left unaddressed at the gate.
    expectations_unaddressed: tuple[str, ...] = tuple()
    # Reproduction-first (step 5): the protocol status for a bug-fix-shaped turn,
    # plus any repro scaffolding still present in the working tree.
    repro_status: str = ""
    repro_artifacts_present: tuple[str, ...] = tuple()
    # Blast radius (step 6): tests that passed in the clean-tree baseline of the
    # selected scope and fail after the change, plus the protocol status.
    blast_radius_status: str = ""
    blast_radius_new_failures: tuple[str, ...] = tuple()

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "problems": list(self.problems),
            "hard_criterion_ids": list(self.hard_criterion_ids),
            "covered_hard_criterion_ids": list(self.covered_hard_criterion_ids),
            "failed_hard_criterion_ids": list(self.failed_hard_criterion_ids),
            "evidence_hierarchy": list(self.evidence_hierarchy),
            "reason": self.reason,
            "regressions": list(self.regressions),
            "unattributed_failures": list(self.unattributed_failures),
            "pre_existing_failures": list(self.pre_existing_failures),
            "agent_authored_failures": list(self.agent_authored_failures),
            "expectations_unaddressed": list(self.expectations_unaddressed),
            "repro_status": self.repro_status,
            "repro_artifacts_present": list(self.repro_artifacts_present),
            "blast_radius_status": self.blast_radius_status,
            "blast_radius_new_failures": list(self.blast_radius_new_failures),
        }


@dataclass(frozen=True)
class CompletionCertificateInput:
    contract: AcceptanceContract | None
    final_text: str
    blocked: bool
    blocker_valid: bool
    material_edit_count: int
    require_material_result: bool
    verification_expected: bool
    verification_attempt_count: int
    last_verification_passed: bool | None
    failed_verification_commands: set[str] = field(default_factory=set)
    expected_verification_commands: set[str] = field(default_factory=set)
    missing_verification_commands: set[str] = field(default_factory=set)
    verification_coverage_stale: bool = False
    accepted_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    # Ordering rule (evidence v2): an execute-posture turn that made mutating
    # edits to a verifiable surface requires a qualifying execution-evidence
    # event that ran AFTER the last such edit. Independent of whether any named
    # verification contract exists.
    execution_evidence_required: bool = False
    post_edit_execution_evidence_present: bool = False
    # Baseline-first regression attribution (step 3). The four lists are the
    # aggregated diff of post-edit test failures vs the same-command pre-edit
    # baseline. ``regression_attribution_supersedes_last_failure`` is True when the
    # last verification attempt was a test run and the diff attributes at least one
    # failure as pre-existing/regression/unattributed — so the specific attribution
    # (below) replaces the generic ``verification_failed``: pre-existing-only does
    # not block (sympy-12489), regressions/unattributed get their own problem.
    regression_baseline_enabled: bool = False
    regressions: tuple[str, ...] = tuple()
    unattributed_failures: tuple[str, ...] = tuple()
    pre_existing_failures: tuple[str, ...] = tuple()
    agent_authored_failures: tuple[str, ...] = tuple()
    regression_attribution_supersedes_last_failure: bool = False
    # Turn-contract v2 (step 4). ``expectations_unaddressed`` are the ids of task
    # expectations (expected-output literals / named loci) that were neither
    # mechanically confirmed nor explicitly disposed. They block as a repairable
    # INSUFFICIENT problem (a nudge, bounded, then an honest UNCONFIRMED marker) —
    # never CONTRADICTED, since a missing disposition is not a proven failure.
    turn_contract_v2_enabled: bool = False
    expectations_unaddressed: tuple[str, ...] = tuple()
    # Reproduction-first (step 5). ``repro_unconfirmed`` is True when the turn is
    # bug-fix-shaped, changed product code, and no reproduction of the *reported*
    # symptom was observed failing before the fix and passing after it. It blocks
    # as a repairable INSUFFICIENT problem — except when the reproduction actively
    # still fails (``repro_failing_after_fix``), which is a proven CONTRADICTED
    # result, the same rank as a detected regression. ``repro_artifacts_present``
    # are recorded scaffolding paths still in the tree at finalization.
    reproduction_first_enabled: bool = False
    repro_unconfirmed: bool = False
    repro_failing_after_fix: bool = False
    repro_status: str = ""
    repro_artifacts_present: tuple[str, ...] = tuple()
    # Blast radius (step 6). ``blast_radius_new_failures`` are tests that passed in
    # the clean-tree baseline of the selected scope and fail after the change: proven
    # collateral damage, so they rank CONTRADICTED alongside a step-3 regression.
    # ``blast_radius_unverified`` is the weaker deficit — the scope around the change
    # was never run after it — and blocks as a repairable INSUFFICIENT problem.
    blast_radius_enabled: bool = False
    blast_radius_new_failures: tuple[str, ...] = tuple()
    blast_radius_unverified: bool = False
    blast_radius_status: str = ""


def evaluate_completion_certificate(
    certificate_input: CompletionCertificateInput,
) -> CompletionCertificate:
    problems: list[str] = []
    failed_hard: list[str] = []
    covered_hard: list[str] = []

    if not str(certificate_input.final_text or "").strip():
        problems.append("empty_final_response")
    if certificate_input.blocked:
        if not certificate_input.blocker_valid:
            problems.append("acceptance_evidence_insufficient")
    elif certificate_input.require_material_result and certificate_input.material_edit_count <= 0:
        problems.append("no_material_edits")

    regression_enabled = certificate_input.regression_baseline_enabled
    has_regressions = regression_enabled and bool(certificate_input.regressions)
    has_unattributed = regression_enabled and bool(certificate_input.unattributed_failures)

    # When attribution supersedes a failed non-contract run (its failures are all
    # pre-existing / regression / unattributed), the generic verification_failed is
    # replaced by the specific attribution below — but the missing/stale coverage
    # checks must still run, so this only suppresses the failed-run branch itself.
    supersede_last_failure = regression_enabled and bool(
        certificate_input.regression_attribution_supersedes_last_failure
    )
    if certificate_input.verification_expected:
        if certificate_input.verification_attempt_count <= 0:
            problems.append("verification_not_attempted")
        elif certificate_input.failed_verification_commands:
            # A named contract command failed: this always blocks. Regression
            # attribution never clears a contract failure (step 2 stays intact).
            problems.append("verification_failed")
        elif certificate_input.last_verification_passed is not True and not supersede_last_failure:
            problems.append("verification_failed")
        elif certificate_input.missing_verification_commands:
            problems.append("verification_incomplete")
        elif certificate_input.verification_coverage_stale:
            problems.append("verification_incomplete")

    # Regression attribution problems are independent of which attempt ran last AND
    # of whether a named verify contract resolved: a regression proven by the
    # agent's own before/after runs must block even when verification_expected is
    # False (e.g. no resolvable verify command). No-edit/advisory turns are exempt
    # structurally (the diff is empty without post-edit runs); blocked
    # finalizations are exempt to stay consistent with step 2.
    if regression_enabled and not certificate_input.blocked:
        if has_regressions:
            problems.append("regressions_detected")
        elif has_unattributed and certificate_input.verification_expected:
            problems.append("unattributed_failures")

    # Turn-contract v2: task-named expectations left unaddressed (neither confirmed
    # by observed evidence nor explicitly disposed) block this execute turn until
    # resolved. Blocked finalizations are exempt (consistent with steps 2-3).
    if (
        certificate_input.turn_contract_v2_enabled
        and not certificate_input.blocked
        and certificate_input.expectations_unaddressed
    ):
        problems.append("expectations_unaddressed")

    # Reproduction-first: a bug-fix-shaped turn that changed product code without a
    # reproduction that failed before the fix and passes after it has not validated
    # the *reported* symptom, only its own reading of it. Blocked finalizations are
    # exempt (consistent with steps 2-4).
    repro_enabled = certificate_input.reproduction_first_enabled
    repro_unconfirmed = repro_enabled and certificate_input.repro_unconfirmed
    if repro_unconfirmed and not certificate_input.blocked:
        problems.append("repro_unconfirmed")
    if (
        repro_enabled
        and not certificate_input.blocked
        and certificate_input.repro_artifacts_present
    ):
        problems.append("repro_artifacts_present")

    # Blast radius: a change that broke tests it was not aiming at has failed the
    # task even when its own verification passes. New failures are proven collateral
    # damage; a scope that was never re-run is an unmeasured blast radius. Blocked
    # finalizations are exempt (consistent with steps 2-5).
    blast_radius_enabled = certificate_input.blast_radius_enabled
    has_blast_radius_regressions = blast_radius_enabled and bool(
        certificate_input.blast_radius_new_failures
    )
    if not certificate_input.blocked and blast_radius_enabled:
        if has_blast_radius_regressions:
            problems.append("blast_radius_regressions")
        elif certificate_input.blast_radius_unverified:
            problems.append("blast_radius_unverified")

    # Ordering rule: post-edit execution evidence. Applies even when no named
    # verification contract exists, catching "edited source, then only ran a
    # syntax check, then finalized". Deduped against the block above.
    if (
        certificate_input.execution_evidence_required
        and not certificate_input.post_edit_execution_evidence_present
    ):
        if certificate_input.verification_attempt_count <= 0:
            problems.append("verification_not_attempted")
        else:
            problems.append("verification_incomplete")

    hard_criteria = _hard_criteria(certificate_input.contract)
    blocker_covers_missing = certificate_input.blocked and certificate_input.blocker_valid
    for criterion in hard_criteria:
        if criterion.status == AcceptanceCriterionStatus.PASSED:
            covered_hard.append(criterion.criterion_id)
            continue
        if criterion.status == AcceptanceCriterionStatus.FAILED:
            failed_hard.append(criterion.criterion_id)
            problems.append("acceptance_criteria_failed")
            if criterion.kind == AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH:
                problems.append("unexpected_scope_changes")
            continue
        if criterion.status == AcceptanceCriterionStatus.BLOCKED:
            if blocker_covers_missing:
                continue
            problems.append("acceptance_evidence_insufficient")
            continue
        if criterion.status == AcceptanceCriterionStatus.UNVERIFIED:
            if blocker_covers_missing:
                continue
            problems.append("acceptance_criteria_unverified")

    status = CompletionCertificateStatus.SUFFICIENT
    reason = "requirements_satisfied"
    deduped_problems = tuple(dict.fromkeys(problems))
    repro_contradicted = bool(
        repro_enabled
        and certificate_input.repro_failing_after_fix
        and "repro_unconfirmed" in deduped_problems
    )
    if (
        failed_hard
        or "verification_failed" in deduped_problems
        or "regressions_detected" in deduped_problems
        or "blast_radius_regressions" in deduped_problems
        or repro_contradicted
    ):
        status = CompletionCertificateStatus.CONTRADICTED
        reason = "hard_requirement_failed"
    elif deduped_problems:
        status = CompletionCertificateStatus.INSUFFICIENT
        reason = "requirements_missing"

    return CompletionCertificate(
        status=status,
        problems=deduped_problems,
        hard_criterion_ids=tuple(criterion.criterion_id for criterion in hard_criteria),
        covered_hard_criterion_ids=tuple(covered_hard),
        failed_hard_criterion_ids=tuple(failed_hard),
        evidence_hierarchy=_evidence_hierarchy(certificate_input.accepted_verification_evidence),
        reason=reason,
        regressions=tuple(certificate_input.regressions) if regression_enabled else tuple(),
        unattributed_failures=(
            tuple(certificate_input.unattributed_failures) if regression_enabled else tuple()
        ),
        pre_existing_failures=(
            tuple(certificate_input.pre_existing_failures) if regression_enabled else tuple()
        ),
        agent_authored_failures=(
            tuple(certificate_input.agent_authored_failures) if regression_enabled else tuple()
        ),
        expectations_unaddressed=(
            tuple(certificate_input.expectations_unaddressed)
            if certificate_input.turn_contract_v2_enabled
            else tuple()
        ),
        repro_status=str(certificate_input.repro_status or "") if repro_enabled else "",
        repro_artifacts_present=(
            tuple(certificate_input.repro_artifacts_present) if repro_enabled else tuple()
        ),
        blast_radius_status=(
            str(certificate_input.blast_radius_status or "") if blast_radius_enabled else ""
        ),
        blast_radius_new_failures=(
            tuple(certificate_input.blast_radius_new_failures) if blast_radius_enabled else tuple()
        ),
    )


def _hard_criteria(contract: AcceptanceContract | None) -> list[AcceptanceCriterion]:
    if contract is None:
        return []
    return [
        criterion
        for criterion in contract.criteria
        if criterion.enforcement == AcceptanceCriterionEnforcement.HARD
        and criterion.required
        and criterion.required_for_finalization
    ]


def _evidence_hierarchy(evidence: list[dict[str, Any]]) -> tuple[str, ...]:
    origins = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        raw = str(
            item.get("origin")
            or item.get("evidence_origin")
            or item.get("evidence_category")
            or item.get("category")
            or ""
        )
        if not raw:
            continue
        origins.add(
            {
                "AUTHORITATIVE": "HOST_AUTHORITATIVE",
                "TASK_ACCEPTANCE": "DIRECT_BLACK_BOX",
                "REPO_NATIVE": "PREEXISTING_REPO_NATIVE",
            }.get(raw, raw)
        )
    return tuple(
        sorted(
            (origin for origin in origins if origin),
            key=lambda origin: (_EVIDENCE_RANK.get(origin, 99), origin),
        )
    )
