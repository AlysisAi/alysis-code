from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..diff_paths import iter_patch_paths
from ..failure_category import FailureCategory, is_infra_unavailable_error
from ..language_policy import normalize_language_name
from ..runtime_kind import RuntimeKind
from ..tools.availability import is_tool_unavailable_result
from ..verification_command_analysis import (
    analyze_verification_command,
    is_benign_non_execution_reason,
)
from ..verify_gate import (
    ResolvedVerifyCommands,
    assess_verification_command_execution,
    extract_actionable_failure_snippet,
    extract_verification_failure_snippet,
    is_authoritative_verify_command_selection,
    is_toolchain_unavailable_verification_output,
    resolve_task_aware_verify_command_selection,
    verification_selection_payload,
)
from ..verify_gate import run_task_verification as run_task_verification
from .acceptance_contract import (
    AcceptanceContract,
    acceptance_contract_problem_payload,
    extract_explicit_acceptance_commands,
    record_acceptance_tool_effect,
)
from .blast_radius import (
    MAX_SCOPE_RUNS,
    BlastRadiusAssessment,
    BlastRadiusPolicy,
    BlastRadiusScope,
    BlastRadiusStatus,
    ScopePhase,
    ScopeRun,
    assess_blast_radius,
    blast_radius_blocks_finalization,
    build_blast_radius_nudge_line,
    classify_scope_phase,
    command_path_selectors,
)
from .completion_certificate import (
    CompletionCertificateInput,
    evaluate_completion_certificate,
)
from .completion_gate import CompletionGateControllerState
from .mutation_classification import classify_mutation_paths, material_mutation_paths
from .prompt_context import (
    _extract_workspace_relation_paths_from_text,
    _normalize_repo_relative_hint_path,
    _paths_require_verification,
    _session_repo_scan,
    _session_task_brief_content,
    _session_verify_command_selection,
    _task_brief_lines_from_text,
    _verification_commands_apply_to_paths,
    refresh_session_environment_context_message,
)
from .regression_baseline import (
    EMPTY_REGRESSION_DIFF,
    BaselineRecord,
    PostEditTestRun,
    RegressionDiffResult,
    TestReport,
    aggregate_regression_results,
    baseline_command_key,
    classify_regression_diff,
    command_is_test_runner,
    parse_test_report,
)
from .reproduction_first import (
    MAX_REPRO_ARTIFACTS,
    MAX_REPRO_RUNS,
    ReproAssessment,
    ReproPhase,
    ReproRun,
    TaskShape,
    assess_reproduction,
    build_repro_artifacts_nudge_line,
    build_repro_nudge_line,
    classify_repro_phase,
    match_repro_artifacts,
    repro_blocks_finalization,
)
from .turn_contract import (
    AdvisoryCompletion,
    DispositionRecord,
    Expectation,
    ExpectationAssessment,
    ExpectationEvidence,
    assess_expectations,
    match_expectation_evidence,
)
from .verification_commands import (
    _matching_effective_verification_commands,
    _normalize_shell_command_for_match,
)
from .verification_evidence import (
    VerificationEvidence,
    VerificationEvidenceCategory,
    classify_verification_evidence,
    command_is_qualifying_execution_evidence,
)

if TYPE_CHECKING:
    from .turn_path import _OneShotRepoTurnIntent


_COMMAND_LIKE_MUTATION_TOOL_NAMES = {"verify_run", "shell_run"}
_MATERIAL_EDIT_TOOL_NAMES = {
    "fs_write",
    "fs_edit",
    "git_apply_patch",
    "fs_move",
    "fs_copy",
    "fs_delete",
    "fs_mkdir",
    "shell_service_start",
    "workspace_preview_start",
}
_VERIFICATION_SHELL_MARKERS = (
    "pytest",
    "py.test",
    "unittest",
    "tox",
    "nox",
    "go test",
    "cargo test",
    "npm test",
    "pnpm test",
    "yarn test",
    "vitest",
    "jest",
    "ruff check",
    "mypy",
    "flake8",
    "pylint",
    "make test",
    "make check",
)
_TEST_EXECUTION_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:pytest|py\.test|tox|nox)(?:\s|$)|"
    r"\b(?:python(?:3)?|py)\s+-m\s+(?:pytest|unittest)\b|"
    r"\b(?:python(?:3)?|py)\b[^\n]*\bmanage\.py\s+test\b|"
    r"\b(?:python(?:3)?|py)\b[^\n]*\b(?:runtests?|test_[^\s/]+|[^\s/]+_test)\.py\b|"
    r"(?:^|\s)(?:\./)?(?:bin/)?(?:runtests?|test)(?:\s|$)|"
    r"\b(?:go|cargo)\s+test\b|"
    r"\b(?:npm|pnpm|yarn)\s+test\b|"
    r"\b(?:vitest|jest|rspec|phpunit|ctest)\b|"
    r"\b(?:mvn|mvnw|maven|gradle|gradlew|dotnet|bazel|mix)\b[^\n]*\btest\b|"
    r"\b(?:make|just)\s+(?:test|check)\b",
    re.IGNORECASE,
)
_TEST_SUCCESS_CLAIM_RE = re.compile(
    r"\b(?:all\s+)?(?:\d+\s+)?tests?(?:\s+suite)?\s+"
    r"(?:(?:is|are|was|were)\s+)?"
    r"(?:pass(?:ed|es|ing)?|succeed(?:ed|s)?|green)\b|"
    r"\btests?\s*:\s*[^\n]{0,120}\b(?:pass(?:ed|es|ing)?|succeed(?:ed|s)?)\b|"
    r"\b(?:pass(?:ed|es|ing)?|green)\s+(?:all\s+)?tests?\b",
    re.IGNORECASE,
)
_GENERIC_VERIFICATION_SUCCESS_CLAIM_RE = re.compile(
    r"\bverified\b|"
    r"\bverification\s+(?:passed|succeeded|completed|was\s+successful)\b|"
    r"\b(?:validation|checks?)\s+(?:passed|succeeded)\b",
    re.IGNORECASE,
)
_NEGATED_CLAIM_PREFIX_RE = re.compile(
    r"(?:\b(?:not|never|no|without)\b[^.!?\n]{0,32}|"
    r"\b(?:cannot|can't|could\s+not|couldn't|did\s+not|didn't|wasn't|isn't|"
    r"unable\s+to|failed\s+to)(?:\s+be)?)\s*$",
    re.IGNORECASE,
)
_SAFE_LEADING_CD_RE = re.compile(
    r"^\s*cd(?:\s+/d)?\s+(?:\"[^\"]*\"|'[^']*'|[^\s]+)\s*&&\s*",
    re.IGNORECASE,
)
_UNSAFE_CLAIM_EVIDENCE_SHELL_RE = re.compile(
    r"\|\||(?<![&])\|(?![&])|;|[\r\n]|&&|(?:^|\s)&(?:\s|$)",
)
_SHELL_REDIRECTION_RE = re.compile(
    r"\s+(?:\d*>&\d+|\d*(?:>>?|<)\s*[^\s]+)(?=\s|$)",
)
SUPPLEMENTAL_VERIFICATION_ADVISORY = (
    "Note: every passing check so far was authored during this session. "
    "Self-written tests verify your interpretation, not the task's. Re-read the "
    "task's exact requirements (output path, format, names, values) and confirm "
    "your deliverable against the spec itself before finalizing."
)
# One-shot advisory emitted at the first verification-relevant edit when no
# baseline exists for any known verification-contract command. Advisory only -
# it never blocks the edit; it teaches the baseline-first protocol so failures
# can later be attributed to the change vs pre-existing breakage.
REGRESSION_BASELINE_PRE_EDIT_ADVISORY = (
    "Baseline advisory: this is your first change to a verifiable surface and no "
    "pre-edit test run is recorded. To let me tell failures your change causes "
    "apart from ones already present in the repo, run the relevant test command "
    "(your verification command) once before further edits. Advisory only - this "
    "does not block your edit."
)
# One-shot advisory emitted the first time a material edit lands inside a
# generated or vendored tree (node_modules, vendor, externals, third_party, ...).
# Advisory only - it never blocks the edit; legitimate vendored fixes exist, but
# in practice edits there are usually a mistargeted change that breaks
# neighboring tests wholesale.
VENDORED_PATH_EDIT_ADVISORY = (
    "Scope advisory: this edit changes files under a generated or vendored tree "
    "({paths}). Vendored/generated code is almost never where the fix belongs - "
    "it is overwritten by upstream syncs and edits there tend to break many "
    "unrelated tests. Prefer the first-party source module; if the vendored copy "
    "truly is the target, keep the change minimal and run the neighboring tests "
    "before finalizing. Advisory only - this does not block your edit."
)
# One-shot finalize-time advisory for execute turns whose completion gate is
# otherwise clear. Small diffs that satisfy the agent's own reproduction are the
# dominant shape of "almost right" outcomes: the issue usually implies more
# behavior (exact message wording, boundary inputs, interactions) than the first
# repro covers. One adversarial pass converts a measurable share of these.
ADVERSARIAL_FINALIZE_REVIEW_ADVISORY = (
    "Adversarial review - one pass before you finish: re-read the original "
    "request end to end and enumerate every behavior it implies, not just the "
    "headline symptom: exact error/message wording, boundary and degenerate "
    "inputs (zero, empty, None, negative), types and units, and every "
    "interaction or API named anywhere in the report. For each implied "
    "behavior, either point at evidence you already ran that covers it, or "
    "extend your reproduction to cover it and run it now. Acceptance checks "
    "usually probe edge semantics beyond the reported case. If everything is "
    "already covered, finalize."
)


def _adversarial_finalize_enabled(cfg: Any | None) -> bool:
    """Kill-switch for the adversarial finalize review (near-miss pass).

    ``ALYSIS_ADVERSARIAL_FINALIZE`` (off/0/false/no/disabled) wins over the
    config value; default is on.
    """
    from ..branding import env_get

    env_value = env_get("ALYSIS_ADVERSARIAL_FINALIZE")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "adversarial_finalize_review", True))


# Maximum bounded repair rounds for a post-edit execution-evidence deficit before
# the gate finalizes honestly-unverified rather than accepting prose in its place.
EVIDENCE_REPAIR_ROUND_BOUND = 2
# Visible marker appended to the final summary when the run finalizes without the
# execution evidence the ordering rule requires (fail honest, never silent).
HONEST_UNVERIFIED_FINALIZATION_MARKER = (
    "\n\n---\n"
    "⚠️ Unverified: I could not obtain a passing test execution after my last "
    "change to the code within this run. This result is finalized as UNVERIFIED — "
    "the change has not been confirmed by running the relevant tests."
)
# Visible marker appended when the run finalizes with regressions the change
# introduced that a bounded action-only repair could not resolve (fail honest).
# Distinct wording from the unverified marker; leads with "REGRESSIONS UNRESOLVED".
_REGRESSIONS_UNRESOLVED_FINALIZATION_MARKER_PREFIX = (
    "\n\n---\n"
    "⛔ REGRESSIONS UNRESOLVED: {ids}. These tests passed in the pre-edit baseline "
    "of `{baseline}` and fail after my change; I could not make them pass within "
    "this run. This result is finalized with KNOWN REGRESSIONS my change introduced."
)


def build_regressions_unresolved_marker(
    regressed_ids: list[str] | tuple[str, ...],
    *,
    baseline_command: str = "",
) -> str:
    ids = ", ".join(str(item) for item in regressed_ids if str(item).strip())
    baseline = str(baseline_command or "").strip() or "the baseline command"
    return _REGRESSIONS_UNRESOLVED_FINALIZATION_MARKER_PREFIX.format(ids=ids, baseline=baseline)


# Visible marker appended when the run finalizes with test failures whose
# relationship to the change could not be established (no comparable baseline).
# Distinct wording again; leads with "UNATTRIBUTED FAILURES".
_UNATTRIBUTED_FAILURES_FINALIZATION_MARKER_PREFIX = (
    "\n\n---\n"
    "⚠️ UNATTRIBUTED FAILURES: {ids}. These tests fail after my change, but I have "
    "no comparable pre-edit baseline for the same command to determine whether my "
    "change caused them. This result is finalized with their cause UNATTRIBUTED — "
    "neither confirmed pre-existing nor confirmed a regression."
)


def build_unattributed_failures_marker(unattributed_ids: list[str] | tuple[str, ...]) -> str:
    ids = ", ".join(str(item) for item in unattributed_ids if str(item).strip())
    return _UNATTRIBUTED_FAILURES_FINALIZATION_MARKER_PREFIX.format(ids=ids)


_COMPLETION_GATE_PROBLEM_LABELS = {
    "empty_final_response": "empty final response",
    "no_material_edits": "no material edits",
    "verification_not_attempted": "verification not attempted",
    "verification_incomplete": "verification coverage incomplete",
    "verification_failed": "verification failing",
    "regressions_detected": "regressions introduced",
    "unattributed_failures": "failures not yet attributed",
    "expectations_unaddressed": "task expectations unaddressed",
    "repro_unconfirmed": "reported symptom not reproduced",
    "repro_artifacts_present": "reproduction scaffolding left in the tree",
    "blast_radius_regressions": "neighbouring tests broken by the change",
    "blast_radius_unverified": "blast radius not measured",
    "acceptance_criteria_unverified": "acceptance criteria unverified",
    "acceptance_criteria_failed": "acceptance criteria failed",
    "acceptance_evidence_insufficient": "acceptance evidence insufficient",
    "unexpected_scope_changes": "unexpected scope changes",
}
_ONE_SHOT_COMPLETION_GATE_NUDGE_PREFIX = (
    "Completion gate: this one-shot execution run cannot finalize yet."
)
_RUNTIME_DEFAULT_LANGUAGE = "english"
_RUNTIME_MESSAGE_CATALOG: dict[str, dict[str, str]] = {
    "english": {
        "phase_understanding_request": "Understanding your request.",
        "phase_drafting_response": "Contacting model provider.",
        "phase_compacted_history": "Compacted conversation history.",
        "phase_retrying_step": "Retrying with higher temperature for this step.",
        "phase_running_tool_steps": "Running {count} tool step(s): {names}.",
        "phase_post_explore_bootstrap": (
            "Detected post-explore stagnation; nudging implementation bootstrap."
        ),
        "phase_exploration_stagnation": (
            "Detected exploration stagnation; nudging toward implementation."
        ),
        "phase_failed_edit_loop": "Detected failed edit loop; nudging strategy switch.",
        "phase_continuing_one_shot": (
            "Continuing one-shot execution after non-final progress update."
        ),
        "phase_continuing_execution": "Continuing execution after non-final progress update.",
        "phase_completion_gate_repair": (
            "Completion gate detected missing execution evidence; requesting action-oriented repair."
        ),
        "phase_optional_finalization_review": (
            "Requirements satisfied; running an optional final review."
        ),
        "phase_step_budget_handoff": (
            "Step budget exhausted; preparing a concise handoff so the chat can continue."
        ),
        "phase_writing_final_response": "Writing the final response.",
        "one_shot_continuation_nudge": (
            "Continue execution now. A text-only plan or progress update is incomplete "
            "for this one-shot run. Use the next required tool action to implement or "
            "create the requested deliverable, run an implementation-producing command, "
            "verify only after material work exists or when the implementation already "
            "exists, or explain a concrete evidence-backed blocker."
        ),
        "interactive_continuation_nudge": (
            "Continue execution now. Do not stop at a planning/progress update. "
            "Use tools to make progress, run relevant verification, or explain a concrete blocker."
        ),
        "one_shot_exploration_nudge": (
            "Avoid repeated read-only exploration. Start implementing or creating the "
            "requested deliverable now, delegate once to a suitable available subagent "
            "if more investigation is genuinely needed, or explain a concrete "
            "evidence-backed blocker."
        ),
        "one_shot_post_explore_bootstrap_nudge": (
            "A subagent already returned useful context in this one-shot turn. You now have enough "
            "context to start implementation. Do not call the same research subagent again "
            "in this turn. Do not use more read-only tools unless there is a concrete blocker. "
            "Your next step must be an implementation or deliverable-creation action "
            "(for example fs_edit, fs_write, git_apply_patch, fs_move, fs_copy, or "
            "shell_run only when it actually performs implementation or creates the "
            "requested deliverable) or a concrete evidence-backed blocker report. "
            "Verification comes after material work exists."
        ),
        "one_shot_post_explore_bootstrap_targets": ("Likely repo-root-relative targets: {joined}."),
        "one_shot_edit_strategy_nudge": (
            "Edit strategy is stuck. Switch approach now: re-read the target lines, then use "
            "fs_edit replace_lines/insert_before_line/insert_after_line with expected_old when "
            "possible, or exact ops replace_exact/insert_before_exact/insert_after_exact when "
            "matching known text. If localized fs_edit is a poor fit, use git_apply_patch or "
            "fs_write. Do not repeat the same failing edit call."
        ),
        "one_shot_non_final_progress_stopped": (
            "One-shot run stopped: model returned repeated/non-final progress text "
            "without continuing implementation."
        ),
        "interactive_non_final_progress_stopped": (
            "Execution turn stopped: model returned repeated/non-final progress text "
            "without continuing implementation."
        ),
        "one_shot_post_explore_retry_exhausted": (
            "One-shot run stopped: post-explore stagnation persisted after bounded "
            "implementation-bootstrap nudges. Start implementing or creating the requested "
            "deliverable now or report a concrete blocker."
        ),
        "one_shot_exploration_retry_exhausted": (
            "One-shot run stopped: exploration stagnation persisted after bounded nudges. "
            "Start implementing or creating the requested deliverable, delegate once to a "
            "suitable available subagent if more "
            "investigation is genuinely needed, or report a concrete blocker."
        ),
        "one_shot_edit_retry_exhausted": (
            "One-shot run stopped: failed edit/write loop persisted after bounded strategy "
            "nudges. Switch to exact-match fs_edit ops, or use git_apply_patch/fs_write, "
            "or report a concrete blocker."
        ),
        "one_shot_post_explore_step_budget_exhausted": (
            "One-shot run stopped: post-explore stagnation consumed the step budget. "
            "Start implementing or creating the requested deliverable now or report a concrete blocker."
        ),
        "one_shot_exploration_step_budget_exhausted": (
            "One-shot run stopped: exploration stagnation consumed the step budget. "
            "Start implementing or creating the requested deliverable, delegate once to a "
            "suitable available subagent if more "
            "investigation is genuinely needed, or report a concrete blocker."
        ),
        "one_shot_edit_step_budget_exhausted": (
            "One-shot run stopped: failed edit/write loop consumed the step budget. "
            "Switch to exact-match fs_edit ops, or use git_apply_patch/fs_write, "
            "or report a concrete blocker."
        ),
        "completion_gate_nudge_prefix": _ONE_SHOT_COMPLETION_GATE_NUDGE_PREFIX,
        "interactive_completion_gate_nudge_prefix": (
            "Completion gate: this interactive execution turn cannot finalize yet."
        ),
        "max_steps_exceeded": "max_steps exceeded",
    },
}


@dataclass
class TurnExecutionState:
    execution_requested: bool
    expected_verification_commands: set[str] = field(default_factory=set)
    covered_verification_commands: set[str] = field(default_factory=set)
    covered_verification_command_generations: dict[str, int] = field(default_factory=dict)
    material_edit_count: int = 0
    material_edit_generation: int = 0
    material_edit_tools: set[str] = field(default_factory=set)
    touched_repo_paths: set[str] = field(default_factory=set)
    last_diff_review_generation: int | None = None
    verification_attempt_count: int = 0
    verification_tools: set[str] = field(default_factory=set)
    last_verification_passed: bool | None = None
    last_verification_failure_snippet: str = ""
    last_verification_failure_category: str = ""
    failed_verification_command_snippets: dict[str, str] = field(default_factory=dict)
    verification_relevant_edit_generation: int = 0
    last_successful_verification_generation: int | None = None
    verification_evidence_counts: dict[str, int] = field(default_factory=dict)
    latest_verification_evidence_category: str = ""
    latest_verification_evidence_reason: str = ""
    accepted_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    supplemental_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    rejected_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    executed_verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    verification_evidence_generation: int = 0
    # Ordering rule: the verification-relevant edit generation at the time of the
    # most recent qualifying execution-evidence event (a real test/execution run,
    # not a syntax-only or static check). Post-edit execution evidence exists when
    # this equals the current verification_relevant_edit_generation. Keying on the
    # verification-relevant generation (not every material edit) means a docs-only
    # edit after a passing run does not re-open the requirement.
    last_post_edit_execution_generation: int | None = None
    completion_gate_repair_attempts: int = 0
    completion_gate_no_material_edits_repair_attempts: int = 0
    completion_gate_missing_verify_repair_attempts: int = 0
    completion_gate_failed_verify_repair_attempts: int = 0
    completion_gate_regression_repair_attempts: int = 0
    completion_gate_unattributed_repair_attempts: int = 0
    completion_gate_expectations_repair_attempts: int = 0
    completion_gate_repro_repair_attempts: int = 0
    # Baseline-first regression protocol (step 3). Baselines are parsed per-test
    # outcomes of runs recorded before the first verification-relevant edit
    # (generation 0), keyed by the normalized executed command. Post-edit runs
    # are compared against the same-command baseline at the completion gate.
    test_baselines: dict[str, BaselineRecord] = field(default_factory=dict)
    post_edit_test_runs: list[PostEditTestRun] = field(default_factory=list)
    agent_created_paths: set[str] = field(default_factory=set)
    regression_baseline_pre_edit_nudge_sent: bool = False
    # True when the most recent verification attempt executed a test-runner
    # command (pytest/unittest). Combined with an all-pre-existing/agent-authored
    # diff, this lets the gate clear a non-contract test failure (the sympy-12489
    # model) without masking a failing non-test command.
    last_verification_attempt_was_test_run: bool = False
    latest_regression_diff: dict[str, Any] = field(default_factory=dict)
    pending_regression_capture_events: list[dict[str, Any]] = field(default_factory=list)
    # Turn-contract v2 (step 4). ``post_edit_run_outputs`` are bounded observed
    # outputs of post-edit runs, the fact surface the expected-output evidence
    # linker substring-matches contract literals against. ``recorded_*`` hold any
    # agent-declared dispositions / advisory-completion reason (unpopulated in this
    # release; the gate synthesizes mechanically — see turn_contract.py).
    post_edit_run_outputs: list[dict[str, Any]] = field(default_factory=list)
    recorded_expectation_dispositions: dict[str, DispositionRecord] = field(default_factory=dict)
    recorded_advisory_completion: AdvisoryCompletion | None = None
    latest_expectation_assessment: dict[str, Any] = field(default_factory=dict)
    latest_expectation_evidence: list[dict[str, Any]] = field(default_factory=list)
    # Reproduction-first (step 5). ``repro_task_shape`` is set once at turn start;
    # ``repro_runs`` are the observed executions of agent-created artifacts, each
    # already phase-classified against the artifact set known at the time. The
    # remaining fields carry the guardrail signals the summary must surface.
    repro_task_shape: TaskShape = TaskShape.OTHER
    repro_runs: list[ReproRun] = field(default_factory=list)
    repro_artifact_paths: set[str] = field(default_factory=set)
    repro_revision_rounds: int = 0
    repro_artifacts_edited_after_fix: set[str] = field(default_factory=set)
    repro_surviving_artifacts: tuple[str, ...] = ()
    repro_pre_edit_nudge_sent: bool = False
    repro_not_reproducing_nudge_sent: bool = False
    repro_edited_after_fix_nudge_sent: bool = False
    latest_repro_assessment: dict[str, Any] = field(default_factory=dict)
    pending_repro_run_events: list[dict[str, Any]] = field(default_factory=list)
    # Blast radius (step 6). ``blast_radius_scope`` is recomputed by the turn loop
    # as the touched-path set grows; ``blast_radius_runs`` are every parsed test run
    # observed this turn, each already tagged with what it selected and whether it
    # ran on the clean tree. Capture is command-agnostic on purpose: the scope is
    # matched by coverage at assessment time, so the agent may run it any way.
    blast_radius_scope: BlastRadiusScope = field(default_factory=BlastRadiusScope)
    blast_radius_runs: list[ScopeRun] = field(default_factory=list)
    blast_radius_policy: BlastRadiusPolicy = field(default_factory=BlastRadiusPolicy)
    blast_radius_scope_advisory_sent: bool = False
    blast_radius_shrink_rounds: int = 0
    completion_gate_blast_radius_repair_attempts: int = 0
    latest_blast_radius_assessment: dict[str, Any] = field(default_factory=dict)
    pending_blast_radius_events: list[dict[str, Any]] = field(default_factory=list)
    completion_gate_controller_state: CompletionGateControllerState = field(
        default_factory=CompletionGateControllerState
    )
    acceptance_contract: AcceptanceContract | None = None
    latest_completion_certificate: dict[str, Any] = field(default_factory=dict)

    def refresh_verification_coverage(self) -> None:
        self.covered_verification_commands = {
            command
            for command, generation in self.covered_verification_command_generations.items()
            if generation == self.verification_relevant_edit_generation
        }

    def note_verification_relevant_edit(self) -> None:
        self.verification_relevant_edit_generation += 1
        self.refresh_verification_coverage()

    def note_material_edit(self) -> None:
        self.material_edit_count += 1
        self.material_edit_generation += 1

    def record_diff_review(self) -> None:
        self.last_diff_review_generation = self.material_edit_generation

    def diff_review_is_stale(self) -> bool:
        return self.material_edit_count > 0 and (
            self.last_diff_review_generation is None
            or self.last_diff_review_generation < self.material_edit_generation
        )

    def record_verification_coverage(self, commands: set[str]) -> None:
        if not commands:
            return
        for command in commands:
            self.covered_verification_command_generations[command] = (
                self.verification_relevant_edit_generation
            )
            self.failed_verification_command_snippets.pop(command, None)
        self.last_successful_verification_generation = self.verification_relevant_edit_generation
        self.refresh_verification_coverage()

    def record_verification_failures(self, failures: dict[str, str]) -> None:
        for command, snippet in failures.items():
            clean_command = str(command or "").strip()
            if not clean_command:
                continue
            clean_snippet = str(snippet or "").strip()
            self.failed_verification_command_snippets[clean_command] = clean_snippet

    def record_verification_evidence(
        self,
        evidence: VerificationEvidence,
        *,
        accepted: bool,
        observed_exit_code: int | None = None,
        observed_output: bool = False,
    ) -> None:
        category = evidence.category.value
        self.verification_evidence_counts[category] = (
            self.verification_evidence_counts.get(category, 0) + 1
        )
        self.latest_verification_evidence_category = category
        self.latest_verification_evidence_reason = evidence.reason
        payload = evidence.as_payload()
        payload["accepted"] = bool(accepted)
        payload["generation"] = self.verification_relevant_edit_generation
        payload["observed_exit_code"] = observed_exit_code
        payload["observed_output"] = bool(observed_output)
        if evidence.real_execution is True:
            self.executed_verification_evidence.append(payload)
            self.executed_verification_evidence[:] = self.executed_verification_evidence[-20:]
        if accepted:
            self.verification_evidence_generation += 1
            self.accepted_verification_evidence.append(payload)
            self.accepted_verification_evidence[:] = self.accepted_verification_evidence[-10:]
        elif evidence.supplemental_only:
            self.supplemental_verification_evidence.append(payload)
            self.supplemental_verification_evidence[:] = self.supplemental_verification_evidence[
                -10:
            ]
        else:
            self.rejected_verification_evidence.append(payload)
            self.rejected_verification_evidence[:] = self.rejected_verification_evidence[-10:]

    def record_executed_command_evidence(
        self,
        *,
        normalized_command: str,
        observed_exit_code: int,
        observed_output: bool,
    ) -> None:
        payload: dict[str, Any] = {
            "evidence_category": "COMMAND_EXECUTION",
            "normalized_command": normalized_command,
            "matched_command": None,
            "real_execution": True,
            "allowed_to_satisfy_contract": False,
            "reason": "observed_shell_verification_execution",
            "covered_verification_commands": [],
            "supplemental_only": False,
            "accepted": False,
            "generation": self.verification_relevant_edit_generation,
            "observed_exit_code": observed_exit_code,
            "observed_output": bool(observed_output),
        }
        self.executed_verification_evidence.append(payload)
        self.executed_verification_evidence[:] = self.executed_verification_evidence[-20:]

    def note_qualifying_execution_evidence(self) -> None:
        self.last_post_edit_execution_generation = self.verification_relevant_edit_generation

    def has_post_edit_execution_evidence(self) -> bool:
        return (
            self.material_edit_count > 0
            and self.last_post_edit_execution_generation is not None
            and self.last_post_edit_execution_generation
            == self.verification_relevant_edit_generation
        )

    def note_agent_created_path(self, path: str) -> None:
        cleaned = str(path or "").strip()
        if cleaned:
            self.agent_created_paths.add(cleaned)

    def has_baseline_for_any(self, commands: list[str] | tuple[str, ...] | set[str] | None) -> bool:
        """True when a usable baseline exists for any of ``commands``."""
        for command in commands or []:
            record = self.test_baselines.get(baseline_command_key(str(command)))
            if record is not None and record.usable:
                return True
        return False

    def note_test_execution(
        self,
        *,
        command: str,
        report: Any,
        timestamp: str = "",
    ) -> None:
        """Record a parsed test run as a baseline (gen 0) or a post-edit run.

        A run recorded before any verification-relevant edit (generation 0) with
        a usable parse is a baseline for its normalized command; a later run is a
        post-edit run. Unparseable pre-edit output is noted for telemetry but can
        never serve as a baseline.
        """
        command_key = baseline_command_key(command)
        if not command_key:
            return
        generation = self.verification_relevant_edit_generation
        if generation == 0:
            if report.usable_as_baseline:
                record = BaselineRecord(
                    command=str(command),
                    command_key=command_key,
                    report=report,
                    edit_generation=0,
                    timestamp=timestamp,
                )
                self.test_baselines[command_key] = record
                self.pending_regression_capture_events.append(
                    {
                        "kind": "baseline",
                        "command": str(command),
                        "command_key": command_key,
                        "edit_generation": 0,
                        "report": report.as_payload(),
                    }
                )
            else:
                self.pending_regression_capture_events.append(
                    {
                        "kind": "baseline_unusable",
                        "command": str(command),
                        "command_key": command_key,
                        "edit_generation": 0,
                        "report": report.as_payload(),
                    }
                )
            return
        run = PostEditTestRun(
            command=str(command),
            command_key=command_key,
            report=report,
            generation=generation,
        )
        self.post_edit_test_runs.append(run)
        self.post_edit_test_runs[:] = self.post_edit_test_runs[-40:]
        self.pending_regression_capture_events.append(
            {
                "kind": "post_edit",
                "command": str(command),
                "command_key": command_key,
                "generation": generation,
                "report": report.as_payload(),
            }
        )

    def current_post_edit_test_runs(self) -> list[PostEditTestRun]:
        """Post-edit runs recorded after the last verification-relevant edit."""
        return [
            run
            for run in self.post_edit_test_runs
            if run.generation == self.verification_relevant_edit_generation
        ]

    def compute_regression_diff(self, *, enabled: bool) -> RegressionDiffResult:
        """Aggregate the same-command diff over current post-edit runs.

        Pure attribution: each current-generation post-edit run is compared only
        against a baseline of the same normalized command. With ``enabled`` off,
        returns the empty diff (legacy gate policy).
        """
        if not enabled:
            self.latest_regression_diff = {}
            return EMPTY_REGRESSION_DIFF
        results = [
            classify_regression_diff(
                post_report=run.report,
                baseline=self.test_baselines.get(run.command_key),
                agent_created_paths=self.agent_created_paths,
            )
            for run in self.current_post_edit_test_runs()
        ]
        aggregate = aggregate_regression_results(results)
        self.latest_regression_diff = aggregate.as_payload()
        return aggregate

    def note_post_edit_run_output(self, *, command: str, output: str, generation: int) -> None:
        """Record a bounded post-edit run output for expectation evidence linking.

        Only runs after a verification-relevant edit (generation > 0) are captured;
        each output is bounded and the buffer is capped, so evidence matching stays
        cheap and never balloons a long turn's state.
        """
        text = str(output or "")
        if not text:
            return
        self.post_edit_run_outputs.append(
            {
                "normalized_command": _normalize_shell_command_for_match(str(command or "")),
                "output": text[:8000],
                "generation": int(generation),
            }
        )
        self.post_edit_run_outputs[:] = self.post_edit_run_outputs[-30:]

    def current_expectation_evidence(
        self, expectations: list[Expectation]
    ) -> list[ExpectationEvidence]:
        """Link expected-output literals to post-edit runs at the current generation."""
        generation = self.verification_relevant_edit_generation
        runs = [
            run
            for run in self.post_edit_run_outputs
            if int(run.get("generation") or 0) >= generation
        ]
        return match_expectation_evidence(expectations, runs)

    def note_repro_run(
        self,
        *,
        command: str,
        artifact_paths: tuple[str, ...],
        exit_code: int | None,
        passed: bool,
    ) -> None:
        """Record one observed execution of an agent-created artifact.

        The phase is resolved *at record time* against the paths the agent created
        this turn, so a run is pre-fix exactly when no pre-existing repo path has
        been modified yet. Writing the repro is itself a material edit, which is
        why the edit generation cannot decide this.
        """
        if not artifact_paths:
            return
        phase, product_paths = classify_repro_phase(
            touched_repo_paths=self.touched_repo_paths,
            created_paths=self.agent_created_paths,
        )
        self.repro_artifact_paths.update(artifact_paths)
        if len(self.repro_artifact_paths) > MAX_REPRO_ARTIFACTS:
            self.repro_artifact_paths = set(sorted(self.repro_artifact_paths)[:MAX_REPRO_ARTIFACTS])
        run = ReproRun(
            command=str(command or ""),
            artifact_paths=tuple(artifact_paths),
            phase=phase,
            passed=bool(passed),
            exit_code=exit_code,
            product_paths=product_paths,
        )
        self.repro_runs.append(run)
        self.repro_runs[:] = self.repro_runs[-MAX_REPRO_RUNS:]
        self.pending_repro_run_events.append(run.as_payload())

    def note_blast_radius_run(
        self,
        *,
        command: str,
        report: Any,
        duration_seconds: float | None = None,
    ) -> None:
        """Record one observed test run for the blast-radius diff (step 6).

        The phase is resolved *at record time* from the edits recorded so far, so a
        run counts as a clean-tree baseline exactly when no pre-existing repo path
        had been modified when it ran. A run made afterwards is never graced into a
        baseline: by then the agent may already have finished its fix, and crediting
        it would mask the very breakage this step exists to catch.
        """
        cleaned = str(command or "").strip()
        if not cleaned:
            return
        phase = classify_scope_phase(
            touched_repo_paths=self.touched_repo_paths,
            created_paths=self.agent_created_paths,
        )
        run = ScopeRun(
            command=cleaned,
            selectors=command_path_selectors(cleaned),
            phase=phase,
            report=report,
            duration_seconds=duration_seconds,
        )
        self.blast_radius_runs.append(run)
        self.blast_radius_runs[:] = self.blast_radius_runs[-MAX_SCOPE_RUNS:]
        self.pending_blast_radius_events.append(run.as_payload())

    def has_blast_radius_baseline(self) -> bool:
        """True when a usable clean-tree run already covers the selected scope."""
        paths = self.blast_radius_scope.paths
        if not paths:
            return False
        return any(
            run.phase == ScopePhase.BASELINE and run.usable and run.covers(paths)
            for run in self.blast_radius_runs
        )

    def compute_blast_radius_assessment(
        self, *, enabled: bool, turn_intent: str
    ) -> BlastRadiusAssessment:
        """Assess the blast-radius gate mechanically (step 6).

        With the feature disabled, on a non-execute turn, or with no scope selected
        (nothing edited yet, or no test surface near the change), returns the empty
        (non-applicable) assessment — the gate then behaves exactly as it did before
        this step.
        """
        applicable = bool(enabled and str(turn_intent or "") == "execute")
        assessment = assess_blast_radius(
            scope=self.blast_radius_scope,
            runs=self.blast_radius_runs,
            applicable=applicable,
            policy=self.blast_radius_policy,
            agent_created_paths=self.agent_created_paths,
        )
        self.latest_blast_radius_assessment = (
            assessment.as_payload() if assessment.applicable else {}
        )
        return assessment

    def note_repro_revision_round(self) -> None:
        self.repro_revision_rounds += 1

    def note_repro_artifact_edited_after_fix(self, paths: set[str] | tuple[str, ...]) -> None:
        for path in paths:
            cleaned = str(path or "").strip()
            if cleaned:
                self.repro_artifacts_edited_after_fix.add(cleaned)

    def repro_protocol_applicable(
        self, *, enabled: bool, turn_intent: str, engagement_based: bool = False
    ) -> bool:
        if not enabled or str(turn_intent or "") != "execute":
            return False
        if self.repro_task_shape == TaskShape.BUG_FIX:
            return True
        if not engagement_based:
            return False
        # Router-free path: no pre-turn task-shape prediction exists. The
        # protocol binds exactly when the agent demonstrably reproduced a
        # failure on the unpatched tree — from then on "the same repro must
        # pass after the fix" is enforceable without interpreting language.
        # Helper scripts that only ever passed never engage the gate.
        return any(run.phase is ReproPhase.PRE_FIX and not run.passed for run in self.repro_runs)

    def compute_repro_assessment(
        self, *, enabled: bool, turn_intent: str, engagement_based: bool = False
    ) -> ReproAssessment:
        """Assess the reproduction protocol mechanically (step 5).

        With the feature disabled, on a non-execute turn, or on a task that reports
        no symptom, returns the empty (non-applicable) assessment — the gate then
        behaves exactly as it did before this step.
        """
        applicable = self.repro_protocol_applicable(
            enabled=enabled,
            turn_intent=turn_intent,
            engagement_based=engagement_based,
        )
        if not applicable:
            self.latest_repro_assessment = {}
            return ReproAssessment()
        assessment = assess_reproduction(
            runs=self.repro_runs,
            applicable=True,
            artifact_paths=self.repro_artifact_paths,
            revision_rounds=self.repro_revision_rounds,
            edited_after_fix=sorted(self.repro_artifacts_edited_after_fix),
            surviving_artifacts=self.repro_surviving_artifacts,
        )
        self.latest_repro_assessment = assessment.as_payload()
        return assessment

    def compute_expectation_assessment(
        self, *, enabled: bool, turn_intent: str
    ) -> ExpectationAssessment:
        """Assess task expectations mechanically at the gate (turn-contract v2).

        Confirmed = an expected-output literal observed in a post-edit run, or a
        named locus that was edited, or an explicit recorded disposition; the rest
        are unaddressed. With the feature disabled, on a non-execute turn, or when
        the contract names no expectations, returns the empty assessment.
        """
        contract = self.acceptance_contract
        expectations = list(contract.expectations) if contract is not None else []
        if not enabled or str(turn_intent or "") != "execute" or not expectations:
            self.latest_expectation_assessment = {}
            self.latest_expectation_evidence = []
            return ExpectationAssessment()
        evidence = self.current_expectation_evidence(expectations)
        assessment = assess_expectations(
            expectations=expectations,
            evidence=evidence,
            edited_loci=self.touched_repo_paths,
            dispositions=self.recorded_expectation_dispositions,
        )
        self.latest_expectation_assessment = assessment.as_payload()
        self.latest_expectation_evidence = [item.as_payload() for item in evidence]
        return assessment

    def missing_verification_commands(self) -> set[str]:
        return self.expected_verification_commands - self.covered_verification_commands

    def failed_verification_commands(self) -> set[str]:
        return set(self.failed_verification_command_snippets) & self.expected_verification_commands

    def first_failed_verification_snippet(self) -> str:
        for command in sorted(self.failed_verification_commands()):
            snippet = self.failed_verification_command_snippets.get(command, "")
            if snippet:
                return snippet
        return ""

    def verification_coverage_is_stale(self) -> bool:
        return (
            bool(self.expected_verification_commands)
            and self.last_successful_verification_generation is not None
            and self.last_successful_verification_generation
            < self.verification_relevant_edit_generation
        )

    def repair_attempts_for_stage(self, stage: str) -> int:
        if stage == "no_material_edits":
            return self.completion_gate_no_material_edits_repair_attempts
        if stage == "verification_not_attempted":
            return self.completion_gate_missing_verify_repair_attempts
        if stage == "verification_incomplete":
            return self.completion_gate_missing_verify_repair_attempts
        if stage == "verification_failed":
            return self.completion_gate_failed_verify_repair_attempts
        if stage == "regressions_detected":
            return self.completion_gate_regression_repair_attempts
        if stage == "unattributed_failures":
            return self.completion_gate_unattributed_repair_attempts
        if stage == "expectations_unaddressed":
            return self.completion_gate_expectations_repair_attempts
        if stage == "repro_unconfirmed":
            return self.completion_gate_repro_repair_attempts
        # Both blast-radius stages share one repair budget: they are two faces of the
        # same protocol, and a run that alternates between them must not get double
        # the rounds.
        if stage in {"blast_radius_regressions", "blast_radius_unverified"}:
            return self.completion_gate_blast_radius_repair_attempts
        return self.completion_gate_repair_attempts

    def increment_repair_attempts_for_stage(self, stage: str) -> None:
        self.completion_gate_repair_attempts += 1
        if stage == "no_material_edits":
            self.completion_gate_no_material_edits_repair_attempts += 1
        elif stage == "verification_not_attempted":
            self.completion_gate_missing_verify_repair_attempts += 1
        elif stage == "verification_incomplete":
            self.completion_gate_missing_verify_repair_attempts += 1
        elif stage == "verification_failed":
            self.completion_gate_failed_verify_repair_attempts += 1
        elif stage == "regressions_detected":
            self.completion_gate_regression_repair_attempts += 1
        elif stage == "unattributed_failures":
            self.completion_gate_unattributed_repair_attempts += 1
        elif stage == "expectations_unaddressed":
            self.completion_gate_expectations_repair_attempts += 1
        elif stage == "repro_unconfirmed":
            self.completion_gate_repro_repair_attempts += 1
        elif stage in {"blast_radius_regressions", "blast_radius_unverified"}:
            self.completion_gate_blast_radius_repair_attempts += 1

    def as_payload(self) -> dict[str, Any]:
        return {
            "execution_requested": self.execution_requested,
            "expected_verification_commands": sorted(self.expected_verification_commands),
            "covered_verification_commands": sorted(self.covered_verification_commands),
            "missing_verification_commands": sorted(self.missing_verification_commands()),
            "material_edit_count": self.material_edit_count,
            "material_edit_generation": self.material_edit_generation,
            "material_edit_tools": sorted(self.material_edit_tools),
            "touched_repo_paths": sorted(self.touched_repo_paths),
            "last_diff_review_generation": self.last_diff_review_generation,
            "diff_review_stale": self.diff_review_is_stale(),
            "verification_attempt_count": self.verification_attempt_count,
            "verification_tools": sorted(self.verification_tools),
            "last_verification_passed": self.last_verification_passed,
            "last_verification_failure_snippet": self.last_verification_failure_snippet,
            "last_verification_failure_category": self.last_verification_failure_category,
            "failed_verification_commands": sorted(self.failed_verification_commands()),
            "verification_relevant_edit_generation": self.verification_relevant_edit_generation,
            "last_successful_verification_generation": self.last_successful_verification_generation,
            "verification_coverage_stale": self.verification_coverage_is_stale(),
            "verification_evidence_counts": dict(sorted(self.verification_evidence_counts.items())),
            "latest_verification_evidence_category": (self.latest_verification_evidence_category),
            "latest_verification_evidence_reason": self.latest_verification_evidence_reason,
            "accepted_verification_evidence": list(self.accepted_verification_evidence),
            "supplemental_verification_evidence": list(self.supplemental_verification_evidence),
            "rejected_verification_evidence": list(self.rejected_verification_evidence),
            "executed_verification_evidence": list(self.executed_verification_evidence),
            "verification_evidence_generation": self.verification_evidence_generation,
            "last_post_edit_execution_generation": self.last_post_edit_execution_generation,
            "post_edit_execution_evidence_present": self.has_post_edit_execution_evidence(),
            "completion_gate_repair_attempts": self.completion_gate_repair_attempts,
            "completion_gate_no_material_edits_repair_attempts": self.completion_gate_no_material_edits_repair_attempts,
            "completion_gate_missing_verify_repair_attempts": self.completion_gate_missing_verify_repair_attempts,
            "completion_gate_failed_verify_repair_attempts": self.completion_gate_failed_verify_repair_attempts,
            "completion_gate_regression_repair_attempts": self.completion_gate_regression_repair_attempts,
            "completion_gate_unattributed_repair_attempts": self.completion_gate_unattributed_repair_attempts,
            "completion_gate_expectations_repair_attempts": self.completion_gate_expectations_repair_attempts,
            "completion_gate_repro_repair_attempts": self.completion_gate_repro_repair_attempts,
            "test_baselines": {
                key: record.as_payload() for key, record in sorted(self.test_baselines.items())
            },
            "post_edit_test_runs": [run.as_payload() for run in self.post_edit_test_runs],
            "agent_created_paths": sorted(self.agent_created_paths),
            "last_verification_attempt_was_test_run": (self.last_verification_attempt_was_test_run),
            "regression_diff": dict(self.latest_regression_diff),
            "expectation_assessment": dict(self.latest_expectation_assessment),
            "expectation_evidence": list(self.latest_expectation_evidence),
            "repro_task_shape": self.repro_task_shape.value,
            "repro_runs": [run.as_payload() for run in self.repro_runs],
            "repro_artifact_paths": sorted(self.repro_artifact_paths),
            "repro_revision_rounds": self.repro_revision_rounds,
            "repro_artifacts_edited_after_fix": sorted(self.repro_artifacts_edited_after_fix),
            "repro_surviving_artifacts": list(self.repro_surviving_artifacts),
            "repro_assessment": dict(self.latest_repro_assessment),
            "completion_gate_blast_radius_repair_attempts": (
                self.completion_gate_blast_radius_repair_attempts
            ),
            "blast_radius_scope": self.blast_radius_scope.as_payload(),
            "blast_radius_runs": [run.as_payload() for run in self.blast_radius_runs],
            "blast_radius_assessment": dict(self.latest_blast_radius_assessment),
            "advisory_completion": (
                self.recorded_advisory_completion.as_payload()
                if self.recorded_advisory_completion is not None
                else None
            ),
            "completion_gate_controller": self.completion_gate_controller_state.as_payload(),
            "completion_gate_last_decision_kind": self.completion_gate_controller_state.last_decision_kind,
            "completion_certificate": dict(self.latest_completion_certificate),
            **acceptance_contract_problem_payload(self.acceptance_contract),
        }

    def acceptance_problem_names(self) -> list[str]:
        if self.acceptance_contract is None:
            return []
        return self.acceptance_contract.problem_names()

    def acceptance_requires_execution(self) -> bool:
        if self.acceptance_contract is None:
            return False
        return any(
            criterion.required_for_finalization
            and criterion.required
            and (
                criterion.commands
                or criterion.thresholds
                or criterion.ports
                or criterion.kind.value
                in {
                    "explicit_command_io",
                    "functional_api_protocol",
                    "persistent_service",
                    "explicit_host_user_verification_command",
                }
            )
            for criterion in self.acceptance_contract.criteria
        )


def _successful_verification_claim_kind(final_text: str) -> str | None:
    text = str(final_text or "")
    for kind, pattern in (
        ("tests", _TEST_SUCCESS_CLAIM_RE),
        ("verification", _GENERIC_VERIFICATION_SUCCESS_CLAIM_RE),
    ):
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 48) : match.start()]
            if _NEGATED_CLAIM_PREFIX_RE.search(prefix):
                continue
            return kind
    return None


def _fresh_executed_evidence_for_claim(
    state: TurnExecutionState,
    *,
    claim_kind: str,
) -> list[dict[str, Any]]:
    required_generation = state.verification_relevant_edit_generation
    evidence: list[dict[str, Any]] = []
    for raw_item in state.executed_verification_evidence:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        try:
            generation = int(item.get("generation"))
        except (TypeError, ValueError):
            continue
        if generation < required_generation:
            continue
        if item.get("real_execution") is not True:
            continue
        if item.get("reason") == "mutated_material_paths":
            continue
        if item.get("observed_exit_code") != 0 or item.get("observed_output") is not True:
            continue
        command = str(item.get("normalized_command") or "").strip()
        if not command:
            continue
        if claim_kind == "tests":
            analysis = analyze_verification_command(command, trusted=True)
            family = str(analysis.command_family or "").casefold()
            if "test" not in family and _TEST_EXECUTION_COMMAND_RE.search(command) is None:
                continue
        evidence.append(item)
    return evidence


def _runtime_message_locale(
    *,
    language: str = "",
    explicit_language_override: bool = False,
) -> str:
    if not explicit_language_override:
        return _RUNTIME_DEFAULT_LANGUAGE
    normalized = normalize_language_name(language).casefold()
    if normalized in _RUNTIME_MESSAGE_CATALOG:
        return normalized
    return _RUNTIME_DEFAULT_LANGUAGE


def _runtime_message(
    key: str,
    *,
    language: str = "",
    explicit_language_override: bool = False,
    **kwargs: Any,
) -> str:
    locale = _runtime_message_locale(
        language=language,
        explicit_language_override=explicit_language_override,
    )
    template = _RUNTIME_MESSAGE_CATALOG.get(locale, {}).get(key)
    if template is None:
        template = _RUNTIME_MESSAGE_CATALOG[_RUNTIME_DEFAULT_LANGUAGE].get(key, key)
    try:
        return template.format(**kwargs)
    except Exception:  # noqa: BLE001
        return template


def _extract_touched_repo_paths(
    *,
    root: Path,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> set[str]:
    normalized_tool = tool_name.strip().lower()
    raw_paths: list[str] = []

    if normalized_tool in {"fs_write", "fs_edit", "fs_delete", "fs_mkdir"}:
        raw_path = result.get("path", arguments.get("path"))
        if isinstance(raw_path, str):
            raw_paths.append(raw_path)
    elif normalized_tool in {"fs_move", "fs_copy"}:
        for key in ("source_path", "destination_path"):
            raw_path = result.get(key, arguments.get(key))
            if isinstance(raw_path, str):
                raw_paths.append(raw_path)
    elif normalized_tool == "git_apply_patch":
        patch = str(arguments.get("patch") or "")
        raw_paths.extend(iter_patch_paths(patch))
    elif normalized_tool == "subagent_run":
        touched_paths = result.get(
            "material_touched_repo_paths",
            result.get("touched_repo_paths"),
        )
        if isinstance(touched_paths, list):
            raw_paths.extend(str(item) for item in touched_paths if isinstance(item, str))
    elif normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        touched_paths = result.get("touched_repo_paths")
        if isinstance(touched_paths, list):
            raw_paths.extend(str(item) for item in touched_paths if isinstance(item, str))

    touched: set[str] = set()
    for raw_path in raw_paths:
        normalized = _normalize_repo_relative_hint_path(root=root, raw=raw_path)
        if normalized:
            touched.add(normalized)
    if normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        return set(material_mutation_paths(touched, root=root))
    return touched


def _verification_attempt_passed(
    *,
    tool_name: str,
    status: str,
    result: dict[str, Any],
    evidence: VerificationEvidence | None = None,
) -> bool:
    if status == "failed":
        return False
    if evidence is not None and not evidence.allowed_to_satisfy_contract:
        return False
    normalized_tool = tool_name.strip().lower()
    touched_repo_paths = result.get("material_touched_repo_paths", result.get("touched_repo_paths"))
    normalized_touched = (
        {str(item) for item in touched_repo_paths if isinstance(item, str) and str(item).strip()}
        if isinstance(touched_repo_paths, list)
        else set()
    )
    if normalized_tool == "verify_run":
        if normalized_touched and _paths_require_verification(normalized_touched):
            return False
        all_passed = result.get("all_passed")
        if isinstance(all_passed, bool):
            return all_passed
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            checks: list[bool] = []
            for item in command_results:
                if not isinstance(item, dict):
                    checks.append(False)
                    continue
                real_execution = item.get("real_execution")
                if real_execution is not True:
                    checks.append(False)
                    continue
                ok = item.get("ok")
                if isinstance(ok, bool):
                    checks.append(ok)
                    continue
                exit_code = item.get("exit_code")
                checks.append(isinstance(exit_code, int) and exit_code == 0)
            return bool(checks) and all(checks)
        return False
    if normalized_tool == "shell_run":
        exit_code = result.get("exit_code")
        if not (isinstance(exit_code, int) and exit_code == 0):
            return False
        if normalized_touched and _paths_require_verification(normalized_touched):
            return False
        output = "\n".join(
            [
                str(result.get("stdout") or "").strip(),
                str(result.get("stderr") or "").strip(),
            ]
        ).strip()
        assessment = assess_verification_command_execution(
            command=str(result.get("effective_cmd") or result.get("cmd") or ""),
            exit_code=exit_code,
            output=output,
        )
        return assessment.real_execution is True
    return False


def _verification_relevant_material_paths(paths: set[str]) -> set[str]:
    if not paths or not _paths_require_verification(paths):
        return set()
    return set(paths)


def _verification_command_result_is_benign_skip(item: dict[str, Any]) -> bool:
    return (
        item.get("status") == "skipped"
        and item.get("ok") is True
        and is_benign_non_execution_reason(str(item.get("non_execution_reason") or ""))
    )


def _verification_command_result_passed(item: dict[str, Any]) -> bool:
    if _verification_command_result_is_benign_skip(item):
        return True
    real_execution = item.get("real_execution")
    if real_execution is not True:
        return False
    ok = item.get("ok")
    if isinstance(ok, bool):
        return ok
    exit_code = item.get("exit_code")
    return isinstance(exit_code, int) and exit_code == 0


def _verification_command_result_snippet(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("output_preview") or "").strip(),
        str(item.get("output") or "").strip(),
        str(item.get("stderr") or "").strip(),
        str(item.get("stdout") or "").strip(),
    ]
    text = "\n".join(part for part in parts if part)
    snippet = extract_actionable_failure_snippet(text)
    return snippet or (text[:240].rstrip() if text else "")


def _verification_failure_category_for_tool_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> str:
    normalized_tool = tool_name.strip().lower()
    if normalized_tool == "verify_run":
        category = str(result.get("failure_category") or "").strip()
        return category or FailureCategory.VERIFICATION_FAILED.value

    if normalized_tool == "shell_run":
        output = "\n".join(
            [
                str(result.get("stdout") or "").strip(),
                str(result.get("stderr") or "").strip(),
            ]
        ).strip()
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        exit_code_raw = result.get("exit_code")
        exit_code = exit_code_raw if isinstance(exit_code_raw, int) else 1
        assessment = assess_verification_command_execution(
            command=command,
            exit_code=exit_code,
            output=output,
        )
        if (
            assessment.non_execution_reason == "execution_layer_failure"
            or is_infra_unavailable_error(output)
            or is_toolchain_unavailable_verification_output(output)
        ):
            return FailureCategory.INFRA_UNAVAILABLE.value

    return FailureCategory.VERIFICATION_FAILED.value


def _record_verify_run_command_outcomes(
    *,
    state: TurnExecutionState,
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
) -> None:
    command_results = result.get("command_results")
    if not isinstance(command_results, list):
        if result.get("all_passed") is True:
            commands = result.get("commands")
            if isinstance(commands, list):
                covered: set[str] = set()
                for command in commands:
                    covered.update(
                        _matching_effective_verification_commands(
                            observed_command=str(command),
                            effective_verification_commands=known_verification_commands,
                        )
                    )
                state.record_verification_coverage(covered)
        return

    covered: set[str] = set()
    failures: dict[str, str] = {}
    for item in command_results:
        if not isinstance(item, dict):
            continue
        matches: set[str] = set()
        observed_candidates = [
            str(item.get("command") or ""),
            str(item.get("effective_command") or ""),
        ]
        for observed in observed_candidates:
            if not observed:
                continue
            matches.update(
                _matching_effective_verification_commands(
                    observed_command=observed,
                    effective_verification_commands=known_verification_commands,
                )
            )
        if not matches:
            continue
        if _verification_command_result_passed(item):
            covered.update(matches)
            continue
        snippet = _verification_command_result_snippet(item)
        for command in matches:
            failures[command] = f"{command}: {snippet}" if snippet else command

    state.record_verification_coverage(covered)
    state.record_verification_failures(failures)


def _record_shell_verification_command_outcome(
    *,
    state: TurnExecutionState,
    arguments: dict[str, Any],
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    passed: bool,
    evidence: VerificationEvidence | None = None,
) -> None:
    matches = (
        set(evidence.covered_verification_commands)
        if evidence is not None and evidence.allowed_to_satisfy_contract
        else set()
    )
    if not matches:
        matches = _matching_effective_verification_commands(
            observed_command=str(result.get("effective_cmd") or arguments.get("cmd") or ""),
            effective_verification_commands=known_verification_commands,
        )
    if not matches:
        return
    if passed:
        state.record_verification_coverage(matches)
        return
    output = "\n".join(
        [
            str(result.get("stdout") or "").strip(),
            str(result.get("stderr") or "").strip(),
        ]
    ).strip()
    snippet = extract_actionable_failure_snippet(output) or output[:240].rstrip()
    state.record_verification_failures(
        {command: f"{command}: {snippet}" if snippet else command for command in matches}
    )


def _verification_output_text(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(result.get("stdout") or "").strip(),
            str(result.get("stderr") or "").strip(),
            str(result.get("output") or "").strip(),
            str(result.get("output_preview") or "").strip(),
        ]
    ).strip()


def _aggregate_verification_evidence(
    records: list[VerificationEvidence],
    *,
    fallback_command: str = "",
) -> VerificationEvidence:
    if not records:
        return VerificationEvidence(
            category=VerificationEvidenceCategory.NOT_VERIFICATION,
            normalized_command=fallback_command,
            reason="no_verification_evidence",
        )
    priority = {
        VerificationEvidenceCategory.AUTHORITATIVE: 0,
        VerificationEvidenceCategory.REPO_NATIVE: 1,
        VerificationEvidenceCategory.TASK_ACCEPTANCE: 2,
        VerificationEvidenceCategory.NOT_VERIFICATION: 3,
    }
    primary = sorted(records, key=lambda item: priority[item.category])[0]
    covered = sorted(
        {command for item in records for command in item.covered_verification_commands if command}
    )
    allowed = bool(records) and all(
        item.allowed_to_satisfy_contract
        for item in records
        if item.category != VerificationEvidenceCategory.NOT_VERIFICATION
    )
    if any(item.category == VerificationEvidenceCategory.NOT_VERIFICATION for item in records):
        allowed = False
    return VerificationEvidence(
        category=primary.category,
        normalized_command=primary.normalized_command,
        matched_command=primary.matched_command,
        real_execution=primary.real_execution,
        allowed_to_satisfy_contract=allowed,
        reason=primary.reason
        if allowed
        else next(
            (item.reason for item in records if not item.allowed_to_satisfy_contract),
            primary.reason,
        ),
        covered_verification_commands=tuple(covered),
        supplemental_only=all(item.supplemental_only for item in records),
    )


def _verification_evidence_note(
    evidence: VerificationEvidence,
    *,
    result: dict[str, Any] | None = None,
) -> str:
    if evidence.category == VerificationEvidenceCategory.NOT_VERIFICATION:
        return ""
    if evidence.supplemental_only:
        return (
            "evidence origin: SELF_AUTHORED "
            "(supplemental - cannot independently confirm spec compliance)"
        )
    result_payload = result if isinstance(result, dict) else {}
    command_specs = result_payload.get("verification_command_specs")
    if isinstance(command_specs, list) and any(
        isinstance(item, dict) and item.get("provenance") == "PREEXISTING_REPO_NATIVE"
        for item in command_specs
    ):
        return "evidence origin: PREEXISTING_REPO_NATIVE (independent)"
    if result_payload.get("verification_contract_type") == "repo_native":
        return "evidence origin: PREEXISTING_REPO_NATIVE (independent)"
    if evidence.category == VerificationEvidenceCategory.AUTHORITATIVE:
        return "evidence origin: USER_EXPLICIT (independent)"
    if evidence.category == VerificationEvidenceCategory.REPO_NATIVE:
        return "evidence origin: PREEXISTING_REPO_NATIVE (independent)"
    if evidence.category == VerificationEvidenceCategory.TASK_ACCEPTANCE:
        return "evidence origin: DIRECT_BLACK_BOX (independent)"
    return ""


def _verify_run_evidence_records(
    *,
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    verification_authoritative: bool,
    material_touched_paths: set[str],
    root: Path,
    evidence_v2: bool = True,
) -> list[VerificationEvidence]:
    command_results = result.get("command_results")
    verification_relevant_touched_paths = _verification_relevant_material_paths(
        material_touched_paths
    )
    records: list[VerificationEvidence] = []
    if isinstance(command_results, list):
        for item in command_results:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or item.get("effective_command") or "")
            if not command:
                continue
            exit_code_raw = item.get("exit_code")
            exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None
            record = classify_verification_evidence(
                command,
                known_verification_commands=known_verification_commands,
                authoritative=verification_authoritative,
                material_touched_paths=verification_relevant_touched_paths,
                exit_code=exit_code,
                output=_verification_output_text(item),
                real_execution=(
                    item.get("real_execution")
                    if isinstance(item.get("real_execution"), bool)
                    or item.get("real_execution") is None
                    else None
                ),
                root=root,
                evidence_v2=evidence_v2,
            )
            if (
                _verification_command_result_is_benign_skip(item)
                and record.category != VerificationEvidenceCategory.NOT_VERIFICATION
                and record.covered_verification_commands
            ):
                record = replace(
                    record,
                    allowed_to_satisfy_contract=True,
                    reason=str(item.get("non_execution_reason") or "verification_skipped"),
                )
            records.append(record)
        return records

    commands = result.get("commands")
    if isinstance(commands, list):
        all_passed = result.get("all_passed")
        exit_code = 0 if all_passed is True else 1 if all_passed is False else None
        for command in commands:
            records.append(
                classify_verification_evidence(
                    str(command),
                    known_verification_commands=known_verification_commands,
                    authoritative=verification_authoritative,
                    material_touched_paths=verification_relevant_touched_paths,
                    exit_code=exit_code,
                    output=_verification_output_text(result),
                    root=root,
                    evidence_v2=evidence_v2,
                )
            )
    return records


def _shell_verification_evidence(
    *,
    root: Path,
    state: TurnExecutionState,
    arguments: dict[str, Any],
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    verification_authoritative: bool,
    material_touched_paths: set[str],
    evidence_v2: bool = True,
) -> VerificationEvidence:
    exit_code_raw = result.get("exit_code")
    exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None
    command = str(result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or "")
    stage_status_raw = result.get("pipeline_stage_status")
    stage_status = (
        [int(item) for item in stage_status_raw]
        if isinstance(stage_status_raw, list)
        and all(isinstance(item, int) for item in stage_status_raw)
        else None
    )
    return classify_verification_evidence(
        command,
        known_verification_commands=known_verification_commands,
        authoritative=verification_authoritative,
        changed_paths=state.touched_repo_paths,
        material_touched_paths=_verification_relevant_material_paths(material_touched_paths),
        exit_code=exit_code,
        output=_verification_output_text(result),
        root=root,
        stage_status=stage_status,
        evidence_v2=evidence_v2,
    )


def _verification_evidence_observation(
    *,
    tool_name: str,
    evidence: VerificationEvidence,
    result: dict[str, Any],
) -> tuple[int | None, bool]:
    def _has_output(payload: dict[str, Any]) -> bool:
        if any(
            str(payload.get(key) or "").strip()
            for key in ("output", "output_preview", "stdout", "stderr")
        ):
            return True
        output_chars = payload.get("output_chars")
        return isinstance(output_chars, int) and output_chars > 0

    normalized_tool = tool_name.strip().casefold()
    if normalized_tool == "shell_run":
        exit_code = result.get("exit_code")
        return (
            exit_code if isinstance(exit_code, int) else None,
            _has_output(result),
        )

    if normalized_tool == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            evidence_command = _normalize_shell_command_for_match(evidence.normalized_command)
            for raw_item in command_results:
                if not isinstance(raw_item, dict):
                    continue
                command = str(raw_item.get("command") or raw_item.get("effective_command") or "")
                effective_command = str(
                    raw_item.get("effective_command") or raw_item.get("command") or ""
                )
                normalized_candidates = {
                    _normalize_shell_command_for_match(command),
                    _normalize_shell_command_for_match(effective_command),
                }
                if evidence_command not in normalized_candidates:
                    continue
                exit_code = raw_item.get("exit_code")
                return (
                    exit_code if isinstance(exit_code, int) else None,
                    _has_output(raw_item),
                )
        all_passed = result.get("all_passed")
        exit_code = 0 if all_passed is True else 1 if all_passed is False else None
        return (
            exit_code,
            _has_output(result),
        )

    return None, False


def _unmasked_shell_verification_command(command: str) -> str:
    candidate = str(command or "").strip()
    while match := _SAFE_LEADING_CD_RE.match(candidate):
        candidate = candidate[match.end() :].strip()
    if not candidate or _UNSAFE_CLAIM_EVIDENCE_SHELL_RE.search(candidate):
        return ""
    analysis_candidate = _SHELL_REDIRECTION_RE.sub("", candidate).strip()
    analysis = analyze_verification_command(analysis_candidate, trusted=True)
    if analysis.command_family is None:
        return ""
    return _normalize_shell_command_for_match(analysis_candidate)


def _tool_effect_has_qualifying_execution(
    *,
    tool_name: str,
    evidence_records: list[VerificationEvidence],
    result: dict[str, Any],
) -> bool:
    """True when a real test/execution run (pass or fail) is observed.

    Qualifying = a recognized test/execution program that actually executed the
    code — a passing run (``real_execution is True``) or a genuine failing run
    (a non-zero exit of a recognized test command). Excludes syntax-only and
    static checks (ast.parse, py_compile, mypy, ruff check) and non-executions
    (no-tests collected, vacuous commands). Used only for the ordering rule.
    """
    for record in evidence_records:
        if record.category == VerificationEvidenceCategory.NOT_VERIFICATION:
            continue
        if record.real_execution is False:
            continue
        if not command_is_qualifying_execution_evidence(record.normalized_command or ""):
            continue
        if record.real_execution is True:
            return True
        observed_exit_code, _observed_output = _verification_evidence_observation(
            tool_name=tool_name,
            evidence=record,
            result=result,
        )
        if observed_exit_code is not None and observed_exit_code != 0:
            return True
    return False


def _regression_capture_timestamp() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001 - a telemetry timestamp must never crash a turn
        return ""


def _iter_executed_test_commands(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> list[tuple[str, str]]:
    """Yield ``(command, output)`` pairs for executed test-runner commands.

    Only commands whose meaningful first stage is pytest or unittest/Django are
    returned — the runners the parsers understand. Other qualifying executions
    (validation scripts, linters) emit no per-test ids and are out of scope.
    """
    pairs: list[tuple[str, str]] = []
    if tool_name == "shell_run":
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        if command and command_is_test_runner(command):
            pairs.append((command, _verification_output_text(result)))
        return pairs
    if tool_name == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or item.get("effective_command") or "")
                if command and command_is_test_runner(command):
                    pairs.append((command, _verification_output_text(item)))
    return pairs


def _iter_executed_commands_with_outcome(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
) -> list[tuple[str, int | None]]:
    """Yield ``(command, exit_code)`` for every command this tool actually ran.

    ``exit_code`` is ``None`` when the runner reported none; a failed tool status
    with no exit code is reported as a non-zero sentinel so a crashed run is never
    mistaken for a passing one.
    """

    def _exit_code(payload: dict[str, Any]) -> int | None:
        raw = payload.get("exit_code")
        if raw is None:
            raw = payload.get("returncode")
        if raw is None:
            return 1 if status == "failed" else None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 1 if status == "failed" else None

    pairs: list[tuple[str, int | None]] = []
    if tool_name == "shell_run":
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        if command:
            pairs.append((command, _exit_code(result)))
        return pairs
    if tool_name == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or item.get("effective_command") or "")
                if command:
                    pairs.append((command, _exit_code(item)))
    return pairs


def _capture_repro_runs(
    *,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
) -> None:
    """Record executions of files the agent created this turn (step 5).

    A command "runs a reproduction" when one of its path tokens is a path the
    agent created this turn — a fact, not an inference about the file's purpose.
    A run with no resolvable exit code is not recorded at all: an unobservable
    outcome can neither confirm nor refute the reproduction.
    """
    if tool_name not in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        return
    if not state.agent_created_paths:
        return
    for command, exit_code in _iter_executed_commands_with_outcome(
        tool_name=tool_name,
        arguments=arguments,
        status=status,
        result=result,
    ):
        artifacts = match_repro_artifacts(command, state.agent_created_paths)
        if not artifacts or exit_code is None:
            continue
        state.note_repro_run(
            command=command,
            artifact_paths=artifacts,
            exit_code=exit_code,
            passed=exit_code == 0,
        )


def _capture_repro_artifact_edits(
    *,
    state: TurnExecutionState,
    touched_paths: set[str],
) -> None:
    """Record edits to a recorded reproduction artifact made after a product edit.

    Called before ``touched_repo_paths`` absorbs this edit, so the "product code
    already changed" test reads only prior edits.
    """
    if not state.repro_artifact_paths or not touched_paths:
        return
    edited_artifacts = touched_paths & state.repro_artifact_paths
    if not edited_artifacts:
        return
    if not (state.touched_repo_paths - state.agent_created_paths):
        return
    state.note_repro_artifact_edited_after_fix(edited_artifacts)


def _capture_regression_test_runs(
    *,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    elapsed_ms: int | None = None,
) -> None:
    timestamp = _regression_capture_timestamp()
    pairs = _iter_executed_test_commands(
        tool_name=tool_name,
        arguments=arguments,
        result=result,
    )
    # A tool call's elapsed time covers everything it ran, so attributing it to a
    # single command is only honest when that call ran exactly one test command.
    duration_seconds: float | None = None
    if elapsed_ms is not None and len(pairs) == 1:
        try:
            duration_seconds = max(0.0, float(elapsed_ms) / 1000.0)
        except (TypeError, ValueError):
            duration_seconds = None
    for command, output in pairs:
        report = parse_test_report(output)
        if tool_name == "verify_run":
            report = _structured_verify_test_report(
                result=result,
                command=command,
                parsed_report=report,
            )
        state.note_test_execution(command=command, report=report, timestamp=timestamp)
        # Blast radius (step 6) reads the same parsed reports but keys them by what
        # each run selected rather than by command identity, so a clean whole-suite
        # run can baseline a scope the agent never named.
        state.note_blast_radius_run(
            command=command,
            report=report,
            duration_seconds=duration_seconds,
        )


def _structured_verify_test_report(
    *,
    result: dict[str, Any],
    command: str,
    parsed_report: TestReport,
) -> TestReport:
    """Prefer host-recorded verify success; raw output only adds parsed detail."""
    command_key = baseline_command_key(command)
    command_results = result.get("command_results")
    if not isinstance(command_results, list):
        return parsed_report
    for raw_item in command_results:
        if not isinstance(raw_item, dict):
            continue
        observed_command = str(raw_item.get("effective_command") or raw_item.get("command") or "")
        if baseline_command_key(observed_command) != command_key:
            continue
        exit_code = raw_item.get("exit_code")
        structured_passed = raw_item.get("ok") is True or (
            result.get("all_passed") is True and exit_code == 0
        )
        if not structured_passed or raw_item.get("real_execution") is False:
            return parsed_report
        runner = parsed_report.runner
        if runner == "unknown":
            lowered = observed_command.casefold()
            runner = "pytest" if "pytest" in lowered else "unittest"
        return TestReport(
            runner=runner,
            passed=parsed_report.passed,
            failed=0,
            skipped=parsed_report.skipped,
            errors=0,
            counts_known=True,
        )
    return parsed_report


def _capture_expectation_run_outputs(
    *,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Capture bounded observed outputs of post-edit runs (turn-contract v2).

    The expected-output evidence linker substring-matches contract literals against
    these outputs. Capture is unconditional telemetry (never kill-switched) and
    only records runs after a verification-relevant edit — pre-edit output can never
    confirm a post-edit expectation.
    """
    generation = state.verification_relevant_edit_generation
    if generation <= 0:
        return
    if tool_name == "shell_run":
        command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        output = _verification_output_text(result)
        if command and output:
            state.note_post_edit_run_output(command=command, output=output, generation=generation)
    elif tool_name == "verify_run":
        command_results = result.get("command_results")
        if isinstance(command_results, list):
            for item in command_results:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or item.get("effective_command") or "")
                output = _verification_output_text(item)
                if command and output:
                    state.note_post_edit_run_output(
                        command=command, output=output, generation=generation
                    )
        else:
            output = _verification_output_text(result)
            commands = result.get("commands")
            command = (
                ", ".join(str(item) for item in commands if item)
                if isinstance(commands, list)
                else ""
            )
            if output:
                state.note_post_edit_run_output(
                    command=command or "verify_run", output=output, generation=generation
                )


def _verification_attempt_executed_test_runner(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    """True when the recorded verification attempt ran a test-runner command."""
    return bool(
        _iter_executed_test_commands(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
        )
    )


def _record_tool_effect(
    *,
    root: Path,
    state: TurnExecutionState,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    result: dict[str, Any],
    known_verification_commands: list[str] | None,
    verification_authoritative: bool = False,
    evidence_v2: bool = True,
    elapsed_ms: int | None = None,
) -> None:
    if is_tool_unavailable_result(result):
        return
    normalized_tool = tool_name.strip().lower()
    touched_paths: set[str] = set()
    benign_runtime_paths: set[str] = set()
    if normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        raw_touched_paths = result.get("touched_repo_paths")
        if isinstance(raw_touched_paths, list):
            classifications = classify_mutation_paths(
                [str(item) for item in raw_touched_paths if isinstance(item, str)],
                root=root,
                command_was_verification=normalized_tool == "verify_run",
            )
            touched_paths = {item.path for item in classifications if item.is_material}
            benign_runtime_paths = {item.path for item in classifications if not item.is_material}
            if touched_paths:
                result["material_touched_repo_paths"] = sorted(touched_paths)
            if benign_runtime_paths:
                result["benign_runtime_paths"] = sorted(benign_runtime_paths)
        else:
            touched_paths = _extract_touched_repo_paths(
                root=root,
                tool_name=normalized_tool,
                arguments=arguments,
                result=result,
            )
    elif status != "failed" and normalized_tool in _MATERIAL_EDIT_TOOL_NAMES:
        touched_paths = _extract_touched_repo_paths(
            root=root,
            tool_name=normalized_tool,
            arguments=arguments,
            result=result,
        )
    elif normalized_tool == "subagent_run":
        touched_paths = _extract_touched_repo_paths(
            root=root,
            tool_name=normalized_tool,
            arguments=arguments,
            result=result,
        )

    if status != "failed" and normalized_tool == "fs_write" and result.get("created") is True:
        # A brand-new file the agent authored this turn: a failing test in it is
        # signal (agent_authored), not a regression. touched_paths already holds
        # the normalized repo-relative path for fs_write.
        for created_path in touched_paths:
            state.note_agent_created_path(created_path)

    # Reproduction-first guardrail (step 5): the reproduction is only evidence
    # while it stays the one that failed before the fix. Editing a recorded
    # artifact once product code has already changed is recorded and surfaced.
    if status != "failed" and normalized_tool in _MATERIAL_EDIT_TOOL_NAMES:
        _capture_repro_artifact_edits(state=state, touched_paths=touched_paths)

    if (status != "failed" and normalized_tool in _MATERIAL_EDIT_TOOL_NAMES) or (
        normalized_tool == "subagent_run" and touched_paths
    ):
        state.note_material_edit()
        state.material_edit_tools.add(normalized_tool)
        state.touched_repo_paths.update(touched_paths)
        if _paths_require_verification(touched_paths):
            state.note_verification_relevant_edit()
    elif normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES and touched_paths:
        state.note_material_edit()
        state.material_edit_tools.add(normalized_tool)
        state.touched_repo_paths.update(touched_paths)
        if _paths_require_verification(touched_paths):
            state.note_verification_relevant_edit()
    elif status != "failed" and normalized_tool == "git_diff":
        state.record_diff_review()

    verification_attempt = False
    evidence_records: list[VerificationEvidence] = []
    evidence = VerificationEvidence(
        category=VerificationEvidenceCategory.NOT_VERIFICATION,
        normalized_command=str(arguments.get("cmd") or ""),
        reason="not_checked",
    )
    if normalized_tool == "verify_run":
        verification_attempt = True
        evidence_records = _verify_run_evidence_records(
            result=result,
            known_verification_commands=known_verification_commands,
            verification_authoritative=verification_authoritative,
            material_touched_paths=touched_paths,
            root=root,
            evidence_v2=evidence_v2,
        )
        evidence = _aggregate_verification_evidence(evidence_records)
    elif normalized_tool == "shell_run":
        evidence = _shell_verification_evidence(
            root=root,
            state=state,
            arguments=arguments,
            result=result,
            known_verification_commands=known_verification_commands,
            verification_authoritative=verification_authoritative,
            material_touched_paths=touched_paths,
            evidence_v2=evidence_v2,
        )
        evidence_records = [evidence]
        verification_attempt = evidence.category != VerificationEvidenceCategory.NOT_VERIFICATION
    if normalized_tool in _COMMAND_LIKE_MUTATION_TOOL_NAMES:
        result["verification_evidence_category"] = evidence.category.value
        result["verification_evidence_reason"] = evidence.reason
        result["verification_evidence_allowed"] = evidence.allowed_to_satisfy_contract
        result["verification_evidence_supplemental_only"] = evidence.supplemental_only
        result["evidence_verdict"] = evidence.evidence_verdict
        verification_note = _verification_evidence_note(evidence, result=result)
        if verification_note:
            result["verification_note"] = verification_note
        if evidence.supplemental_only:
            result["verification_supplemental_only_note"] = SUPPLEMENTAL_VERIFICATION_ADVISORY
    record_acceptance_tool_effect(
        contract=state.acceptance_contract,
        root=root,
        tool_name=normalized_tool,
        arguments=arguments,
        status=status,
        result=result,
        touched_paths=touched_paths,
        known_verification_commands=known_verification_commands,
        verification_authoritative=verification_authoritative,
        evidence_category=evidence.category.value,
        evidence_allowed=evidence.allowed_to_satisfy_contract,
    )
    # Baseline-first regression protocol (step 3): capture parsed per-test
    # outcomes for baseline/attribution. Runs for every executed test-runner
    # command regardless of the evidence classifier's verdict (so an
    # unobservable-pipeline run still contributes what its output shows), and
    # regardless of the kill-switch (capture is telemetry; only the gate policy
    # is gated).
    _capture_regression_test_runs(
        state=state,
        tool_name=normalized_tool,
        arguments=arguments,
        result=result,
        elapsed_ms=elapsed_ms,
    )
    # Turn-contract v2 (step 4): capture post-edit run output for the expected-output
    # evidence linker. Like the regression capture above, this is unconditional
    # telemetry (only the gate policy is kill-switched).
    _capture_expectation_run_outputs(
        state=state,
        tool_name=normalized_tool,
        arguments=arguments,
        result=result,
    )
    # Reproduction-first (step 5): capture every executed command that runs a file
    # the agent created this turn, phase-classified against the product edits
    # recorded so far. Unconditional telemetry, like the captures above — only the
    # gate policy and the turn directives are kill-switched.
    _capture_repro_runs(
        state=state,
        tool_name=normalized_tool,
        arguments=arguments,
        status=status,
        result=result,
    )
    if normalized_tool == "shell_run" and not verification_attempt:
        raw_command = str(
            result.get("effective_cmd") or result.get("cmd") or arguments.get("cmd") or ""
        )
        normalized_command = _unmasked_shell_verification_command(raw_command)
        observed_exit_code, observed_output = _verification_evidence_observation(
            tool_name=normalized_tool,
            evidence=evidence,
            result=result,
        )
        if normalized_command and observed_exit_code == 0 and observed_output:
            state.record_executed_command_evidence(
                normalized_command=normalized_command,
                observed_exit_code=observed_exit_code,
                observed_output=observed_output,
            )
    if not verification_attempt:
        return

    state.verification_attempt_count += 1
    state.verification_tools.add(normalized_tool)
    state.last_verification_passed = _verification_attempt_passed(
        tool_name=normalized_tool,
        status=status,
        result=result,
        evidence=evidence,
    )
    for record in evidence_records:
        observed_exit_code, observed_output = _verification_evidence_observation(
            tool_name=normalized_tool,
            evidence=record,
            result=result,
        )
        state.record_verification_evidence(
            record,
            accepted=(
                state.last_verification_passed is True and record.allowed_to_satisfy_contract
            ),
            observed_exit_code=observed_exit_code,
            observed_output=observed_output,
        )
    if evidence_v2 and _tool_effect_has_qualifying_execution(
        tool_name=normalized_tool,
        evidence_records=evidence_records,
        result=result,
    ):
        # Ordering rule: stamp that a real execution run happened after the most
        # recent material edit, so finalization can require post-edit evidence.
        state.note_qualifying_execution_evidence()
    # Baseline-first regression protocol (step 3): remember whether this
    # verification attempt ran a test-runner command, so the gate can clear a
    # non-contract all-pre-existing test failure without masking a non-test one.
    state.last_verification_attempt_was_test_run = _verification_attempt_executed_test_runner(
        tool_name=normalized_tool,
        arguments=arguments,
        result=result,
    )
    if normalized_tool == "verify_run":
        _record_verify_run_command_outcomes(
            state=state,
            result=result,
            known_verification_commands=known_verification_commands,
        )
    elif normalized_tool == "shell_run":
        _record_shell_verification_command_outcome(
            state=state,
            arguments=arguments,
            result=result,
            known_verification_commands=known_verification_commands,
            passed=state.last_verification_passed is True,
            evidence=evidence,
        )

    if state.last_verification_passed is True:
        state.last_verification_failure_category = ""
        if not state.failed_verification_commands():
            state.last_verification_failure_snippet = ""
    else:
        state.last_verification_failure_category = _verification_failure_category_for_tool_result(
            tool_name=normalized_tool,
            arguments=arguments,
            result=result,
        )
        state.last_verification_failure_snippet = (
            extract_verification_failure_snippet(
                tool_name=normalized_tool,
                result=result,
            )
            or state.first_failed_verification_snippet()
        )


def _verification_expected_for_turn(
    *,
    turn_intent: _OneShotRepoTurnIntent,
    blocked: bool,
    touched_repo_paths: set[str],
    verification_contract_requires_execution: bool = False,
    verification_contract_available: bool = True,
    effective_verification_commands: list[str] | tuple[str, ...] | set[str] | None = None,
) -> bool:
    if turn_intent != "execute":
        return False
    if verification_contract_requires_execution:
        return True
    if blocked:
        return False
    if not verification_contract_available:
        return False
    return _verification_commands_apply_to_paths(
        touched_repo_paths,
        effective_verification_commands,
    )


def _completion_gate_blocker_allows_final(
    *,
    state: TurnExecutionState,
    blocked_response: bool,
) -> bool:
    if not blocked_response:
        return False
    if not state.touched_repo_paths or not _paths_require_verification(state.touched_repo_paths):
        return True
    if state.verification_attempt_count <= 0:
        return False
    if state.last_verification_passed is True:
        return True
    return state.last_verification_failure_category == FailureCategory.INFRA_UNAVAILABLE.value


def _execution_evidence_required_for_turn(
    *,
    state: TurnExecutionState,
    turn_intent: str,
    blocked: bool,
    evidence_v2: bool,
    verification_expected: bool,
) -> bool:
    """Ordering rule trigger: an execute turn that mutated a verifiable surface.

    The point of the rule is to catch "edited source, then only ran a syntax
    check (or nothing), then finalized". It requires that verification is
    actually applicable for this turn (``verification_expected``) so greenfield
    workspaces with no test surface are not harassed. Turns with no mutating
    edits (pure Q&A/analysis/advisory) are exempt, as are non-execute turns and
    blocker finalizations.
    """
    return bool(
        evidence_v2
        and verification_expected
        and str(turn_intent or "") == "execute"
        and not blocked
        and state.material_edit_count > 0
        and _paths_require_verification(state.touched_repo_paths)
    )


def _completion_gate_problems(
    *,
    state: TurnExecutionState,
    final_text: str,
    blocked: bool,
    verification_expected: bool,
    require_material_edit_evidence: bool = True,
    evidence_v2: bool = False,
    turn_intent: str = "",
    regression_baseline_enabled: bool = False,
    turn_contract_v2_enabled: bool = False,
    reproduction_first_enabled: bool = False,
    repro_engagement_based: bool = False,
    blast_radius_enabled: bool = False,
) -> list[str]:
    expectation_assessment = state.compute_expectation_assessment(
        enabled=turn_contract_v2_enabled,
        turn_intent=turn_intent,
    )
    repro_assessment = state.compute_repro_assessment(
        enabled=reproduction_first_enabled,
        turn_intent=turn_intent,
        engagement_based=repro_engagement_based,
    )
    blast_radius_assessment = state.compute_blast_radius_assessment(
        enabled=blast_radius_enabled,
        turn_intent=turn_intent,
    )
    execution_evidence_required = _execution_evidence_required_for_turn(
        state=state,
        turn_intent=turn_intent,
        blocked=blocked,
        evidence_v2=evidence_v2,
        verification_expected=verification_expected,
    )
    regression_diff = state.compute_regression_diff(enabled=regression_baseline_enabled)
    # Let attribution supersede a non-contract "last attempt failed" block only
    # when that last attempt was itself a test run AND the diff attributes at
    # least one failure as pre-existing/regression/unattributed. The test-run
    # guard stops an all-benign earlier run from masking a failing non-test
    # command; the "not agent-authored-only" guard keeps a failing repro the agent
    # just wrote (agent-authored only) blocking as a generic verification failure.
    regression_attribution_supersedes_last_failure = bool(
        regression_baseline_enabled
        and state.last_verification_attempt_was_test_run
        and (
            regression_diff.regressions
            or regression_diff.unattributed
            or regression_diff.pre_existing
        )
    )
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=state.acceptance_contract,
            final_text=final_text,
            blocked=blocked,
            blocker_valid=blocked,
            material_edit_count=state.material_edit_count,
            require_material_result=require_material_edit_evidence,
            verification_expected=verification_expected,
            verification_attempt_count=state.verification_attempt_count,
            last_verification_passed=state.last_verification_passed,
            failed_verification_commands=state.failed_verification_commands(),
            expected_verification_commands=set(state.expected_verification_commands),
            missing_verification_commands=state.missing_verification_commands(),
            verification_coverage_stale=state.verification_coverage_is_stale(),
            accepted_verification_evidence=list(state.accepted_verification_evidence),
            execution_evidence_required=execution_evidence_required,
            post_edit_execution_evidence_present=state.has_post_edit_execution_evidence(),
            regression_baseline_enabled=regression_baseline_enabled,
            regressions=regression_diff.regressions,
            unattributed_failures=regression_diff.unattributed,
            pre_existing_failures=regression_diff.pre_existing,
            agent_authored_failures=regression_diff.agent_authored,
            regression_attribution_supersedes_last_failure=(
                regression_attribution_supersedes_last_failure
            ),
            turn_contract_v2_enabled=turn_contract_v2_enabled,
            expectations_unaddressed=expectation_assessment.unaddressed,
            reproduction_first_enabled=reproduction_first_enabled,
            repro_unconfirmed=repro_blocks_finalization(
                repro_assessment,
                material_edit_count=state.material_edit_count,
            ),
            repro_failing_after_fix=repro_assessment.contradicted,
            repro_status=repro_assessment.status.value if repro_assessment.applicable else "",
            repro_artifacts_present=repro_assessment.surviving_artifacts,
            blast_radius_enabled=blast_radius_enabled,
            # Failures step 3 already reports as regressions of the same command are
            # dropped here: one fact, one blocker. What remains is the breakage only
            # the selected scope saw — the tests the agent never chose to run.
            blast_radius_new_failures=tuple(
                test_id
                for test_id in blast_radius_assessment.new_failures
                if test_id not in set(regression_diff.regressions)
            ),
            # Only the "never measured" state feeds the weaker problem; a REGRESSED
            # assessment whose ids step 3 already owns must not resurface here as a
            # coverage complaint about a scope the agent demonstrably ran.
            blast_radius_unverified=(
                blast_radius_assessment.status == BlastRadiusStatus.GATE_MISSING
                and blast_radius_blocks_finalization(
                    blast_radius_assessment,
                    material_edit_count=state.material_edit_count,
                )
            ),
            blast_radius_status=(
                blast_radius_assessment.status.value if blast_radius_assessment.applicable else ""
            ),
        )
    )
    state.latest_completion_certificate = certificate.as_payload()
    return list(certificate.problems)


def _sorted_missing_verification_commands(state: TurnExecutionState) -> list[str]:
    return sorted(state.missing_verification_commands())


def _completion_gate_problem_summary(problems: list[str]) -> str:
    labels = [_COMPLETION_GATE_PROBLEM_LABELS.get(item, item) for item in problems]
    return ", ".join(labels) if labels else "unknown completion gate failure"


def _completion_gate_repair_stage(problems: list[str]) -> str:
    if "no_material_edits" in problems:
        return "no_material_edits"
    # Regressions are the most specific, most actionable verification failure:
    # named tests that passed pre-edit and now fail. Rank them ahead of the
    # generic verification_failed so the repair nudge names them concretely.
    if "regressions_detected" in problems:
        return "regressions_detected"
    # Proven collateral damage ranks with the other regression stages and above the
    # generic verification failure: it names concrete tests, and its repair is a
    # different action (narrow the change) than "make your own check pass".
    if "blast_radius_regressions" in problems:
        return "blast_radius_regressions"
    if "verification_failed" in problems:
        return "verification_failed"
    if "verification_incomplete" in problems:
        return "verification_incomplete"
    if "verification_not_attempted" in problems:
        return "verification_not_attempted"
    if "unattributed_failures" in problems:
        return "unattributed_failures"
    # An unmeasured blast radius ranks *below* the verification stages: when nothing
    # has been run at all, "you ran no tests" is the more fundamental complaint and
    # owns the repair loop. This stage takes over once that is satisfied and only the
    # neighbouring tests are still unrun.
    if "blast_radius_unverified" in problems:
        return "blast_radius_unverified"
    # Reproduction-first: an unvalidated reported symptom ranks above the
    # task-expectation stage — a reproduction is the most direct evidence that the
    # delivered change addresses what was reported, not the agent's reading of it.
    # Scaffolding cleanup shares the stage; the nudge names whichever applies.
    if "repro_unconfirmed" in problems or "repro_artifacts_present" in problems:
        return "repro_unconfirmed"
    # Turn-contract v2: task-named expectations rank below verification/regression
    # deficits (broken behavior is more urgent) but above acceptance-criteria stages.
    if "expectations_unaddressed" in problems:
        return "expectations_unaddressed"
    if "acceptance_criteria_failed" in problems or "unexpected_scope_changes" in problems:
        return "acceptance_failed"
    if (
        "acceptance_criteria_unverified" in problems
        or "acceptance_evidence_insufficient" in problems
    ):
        return "acceptance_unverified"
    if "empty_final_response" in problems:
        return "empty_final_response"
    return "generic"


_LIVE_BACKGROUND_PROCESS_FINALIZATION_LINE = (
    "- You have {n} background process(es) started with shell_background; they are "
    "terminated when this run ends. If the task requires a server/daemon to still "
    "be running after you finish, start it with shell_service_start (durable) instead, "
    "and re-verify."
)


def _live_background_process_finalization_advisory_line(
    *,
    one_shot_execution: bool,
    live_background_processes: int = 0,
) -> str:
    try:
        count = int(live_background_processes)
    except (TypeError, ValueError):
        count = 0
    if not one_shot_execution or count <= 0:
        return ""
    return _LIVE_BACKGROUND_PROCESS_FINALIZATION_LINE.format(n=count)


def _completion_gate_nudge_message(
    problems: list[str],
    *,
    prefix_key: str = "completion_gate_nudge_prefix",
    verification_failure_snippet: str = "",
    missing_verification_commands: list[str] | None = None,
    verification_coverage_stale: bool = False,
    anchor_paths: list[str] | None = None,
    has_material_edits: bool = False,
    all_verification_evidence_self_authored: bool = False,
    diff_review_stale: bool = False,
    language: str = "",
    explicit_language_override: bool = False,
    one_shot_execution: bool = False,
    live_background_processes: int = 0,
    execution_evidence_missing_detail: str = "",
    regression_ids: list[str] | None = None,
    regression_baseline_command: str = "",
    unattributed_ids: list[str] | None = None,
    expectation_details: list[str] | None = None,
    repro_assessment: ReproAssessment | None = None,
    blast_radius_assessment: BlastRadiusAssessment | None = None,
) -> str:
    _ = (
        prefix_key,
        verification_coverage_stale,
        anchor_paths,
        language,
        explicit_language_override,
    )
    problem_set = set(problems)
    # A post-edit execution-evidence deficit is action-only: the model must run
    # the tests; a written explanation cannot clear it. Naming the concrete
    # missing fact keeps successive nudges specific rather than repetitive.
    evidence_deficit = bool(execution_evidence_missing_detail) and bool(
        problem_set & {"verification_not_attempted", "verification_incomplete"}
    )
    # Regressions are action-only in the same way: only making the named tests
    # pass again clears the deficit; prose cannot. Unattributed failures need a
    # fact (a rerun of the baseline-known command) to be attributed.
    regression_deficit = "regressions_detected" in problem_set
    unattributed_deficit = "unattributed_failures" in problem_set
    # Turn-contract v2: task-named expectations neither confirmed nor disposed. Each
    # is addressed by editing the named locus or producing (and running) the expected
    # output — prose alone cannot clear it.
    expectation_deficit = "expectations_unaddressed" in problem_set
    # Reproduction-first: an unvalidated reported symptom is action-only too — only
    # a reproduction that failed before the fix and passes after it clears it.
    repro_deficit = "repro_unconfirmed" in problem_set
    repro_artifacts_deficit = "repro_artifacts_present" in problem_set
    # Blast radius: both states are action-only. Only running the scope measures it,
    # and only making the broken tests pass again (by narrowing the change) clears a
    # regression; neither can be talked away.
    blast_radius_deficit = bool(
        problem_set & {"blast_radius_regressions", "blast_radius_unverified"}
    )
    lines = ["Finalization check - one pass before you finish:"]
    if "no_material_edits" in problem_set:
        lines.append(
            "- No file changes are recorded yet. If the task required creating/modifying "
            "something, do it now; if you concluded no change is needed, say so explicitly "
            "with your reasoning."
        )
    snippet = extract_actionable_failure_snippet(verification_failure_snippet)
    if "verification_failed" in problem_set:
        failure_detail = snippet or "the latest verification attempt did not pass"
        lines.append(
            f"- Your last verification failed: {failure_detail}. Fix and re-run, or explain "
            "why the failure is expected/out of scope."
        )
    if missing_verification_commands and (
        "verification_not_attempted" in problem_set or "verification_incomplete" in problem_set
    ):
        lines.append(
            "- Expected verification not yet run: "
            + ", ".join(missing_verification_commands)
            + ". Run them, or state why they don't apply."
        )
    elif "verification_not_attempted" in problem_set or "verification_incomplete" in problem_set:
        lines.append(
            "- Expected verification has not been completed. Run it, or state why it does not apply."
        )
    if evidence_deficit:
        lines.append(
            f"- No test execution recorded {execution_evidence_missing_detail}. Run the relevant "
            "tests now and observe their output and exit code. A written explanation cannot "
            "clear this - only a new test run can."
        )
    if regression_deficit and regression_ids:
        baseline = str(regression_baseline_command or "").strip() or "the baseline command"
        lines.append(
            "- Regressions your change introduced: "
            + ", ".join(str(item) for item in regression_ids)
            + f". These tests passed in the pre-edit baseline of `{baseline}` and now fail. "
            "Fix them and re-run so they pass again. A written explanation cannot clear this - "
            "only making the tests pass can."
        )
    if unattributed_deficit and unattributed_ids:
        lines.append(
            "- Failures with no comparable pre-edit baseline (cannot tell if your change caused "
            "them): "
            + ", ".join(str(item) for item in unattributed_ids)
            + ". Re-run the exact command you have a baseline for (or run it now to establish "
            "one) so these can be attributed, or state their relationship to your change with "
            "evidence."
        )
    if expectation_deficit and expectation_details:
        lines.append(
            "- Task expectations not yet addressed: "
            + "; ".join(str(item) for item in expectation_details)
            + ". The task named these concretely. Either make your change satisfy each one "
            "(edit the named locus, or produce the expected output and run the command so it "
            "is observed), or explicitly state why the expectation no longer applies."
        )
    if repro_deficit and repro_assessment is not None:
        repro_line = build_repro_nudge_line(repro_assessment)
        if repro_line:
            lines.append(repro_line)
    if repro_artifacts_deficit and repro_assessment is not None:
        artifacts_line = build_repro_artifacts_nudge_line(repro_assessment.surviving_artifacts)
        if artifacts_line:
            lines.append(artifacts_line)
    if blast_radius_deficit and blast_radius_assessment is not None:
        blast_radius_line = build_blast_radius_nudge_line(blast_radius_assessment)
        if blast_radius_line:
            lines.append(blast_radius_line)
    if all_verification_evidence_self_authored:
        lines.append(f"- {SUPPLEMENTAL_VERIFICATION_ADVISORY}")
    if has_material_edits and diff_review_stale:
        lines.append(
            "- Consider reviewing the current diff for accidental scope or quality issues before "
            "finalizing."
        )
    live_background_process_line = _live_background_process_finalization_advisory_line(
        one_shot_execution=one_shot_execution,
        live_background_processes=live_background_processes,
    )
    if live_background_process_line:
        lines.append(live_background_process_line)
    lines.append(
        "- Re-read the task statement once and confirm every explicitly named output "
        "(paths, formats, values) exists exactly as requested."
    )
    if (
        evidence_deficit
        or regression_deficit
        or unattributed_deficit
        or expectation_deficit
        or repro_deficit
        or blast_radius_deficit
    ):
        lines.append(
            "Run the relevant tests now, then give your final answer once you have observed "
            "the result."
        )
    else:
        lines.append(
            "Then give your final answer. If you are confident the work is complete as-is, "
            "finalize - this checklist is advisory."
        )
    return "\n".join(lines)


def _build_interactive_turn_verify_task(
    *,
    session: Any,
    instruction: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    task_paths = _extract_workspace_relation_paths_from_text(root=session.root, text=instruction)
    task_brief = _session_task_brief_content(session)
    if task_brief:
        for path in _extract_workspace_relation_paths_from_text(root=session.root, text=task_brief):
            if path not in task_paths:
                task_paths.append(path)
    task_texts = [str(instruction or "").strip()]
    if task_brief:
        task_texts.extend(_task_brief_lines_from_text(task_brief, max_lines=6))
    task_texts = [text for text in task_texts if text]
    if not task_paths and not task_texts:
        return None, []
    task: dict[str, Any] = {}
    if task_paths:
        task["estimated_files"] = list(task_paths)
        task["write_scope"] = list(task_paths)
    if task_texts:
        task["acceptance_criteria"] = list(task_texts)
    return task, task_texts


def _refresh_execute_turn_verification_selection(
    session: Any,
    *,
    instruction: str,
    route_execution_posture: str,
) -> None:
    if not bool(getattr(session, "verification_enabled", True)):
        return
    runtime_kind = getattr(session, "runtime_kind", RuntimeKind.INTERACTIVE_CHAT)
    one_shot_execution = bool(getattr(session, "one_shot_execution", False))
    if runtime_kind != RuntimeKind.INTERACTIVE_CHAT and not one_shot_execution:
        return
    if getattr(session, "authoritative_verification_commands", None) is not None:
        return
    if str(route_execution_posture or "").strip().lower() != "execute":
        return

    repo_scan = _session_repo_scan(session)
    task, plan_requirements = _build_interactive_turn_verify_task(
        session=session,
        instruction=instruction,
    )
    current = _session_verify_command_selection(session)
    resolved = resolve_task_aware_verify_command_selection(
        cfg=session.cfg,
        verify_cmd=None,
        task=task,
        root=session.root,
        repo_scan=repo_scan,
        plan_requirements=plan_requirements,
        selection=current,
    )
    explicit_commands = extract_explicit_acceptance_commands(
        instruction,
        *[str(item) for item in plan_requirements],
    )
    if (
        explicit_commands
        and not is_authoritative_verify_command_selection(current)
        and resolved.contract_type in {"generic_fallback", "unavailable", ""}
    ):
        resolved = ResolvedVerifyCommands(
            commands=tuple(explicit_commands),
            source="task_refinement.explicit_user_command",
            reason="explicit user command is the task-native verification contract",
            contract_type="task_acceptance",
        )
    if (
        current is not None
        and current.commands == resolved.commands
        and current.source == resolved.source
        and current.reason == resolved.reason
        and current.contract_type == resolved.contract_type
    ):
        return

    previous_payload = (
        verification_selection_payload(
            current,
            authoritative=is_authoritative_verify_command_selection(current),
        )
        if current is not None
        else None
    )
    session.effective_verification_commands = list(resolved.commands)
    session.verification_selection_source = resolved.source
    session.verification_selection_reason = resolved.reason
    session.verification_contract_type = resolved.contract_type
    session.verification_authoritative = is_authoritative_verify_command_selection(resolved)
    refresh_session_environment_context_message(session)
    payload: dict[str, Any] = {
        "instruction_paths": list(task.get("estimated_files", []))
        if isinstance(task, dict)
        else [],
        "route_execution_posture": route_execution_posture,
        **verification_selection_payload(
            resolved,
            authoritative=is_authoritative_verify_command_selection(resolved),
        ),
    }
    if previous_payload is not None:
        payload["previous"] = previous_payload
    session.store.append("verification_contract_updated", payload)


def _refresh_interactive_turn_verification_selection(
    session: Any,
    *,
    instruction: str,
    route_execution_posture: str,
) -> None:
    _refresh_execute_turn_verification_selection(
        session,
        instruction=instruction,
        route_execution_posture=route_execution_posture,
    )
