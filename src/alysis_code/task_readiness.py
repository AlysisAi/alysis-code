from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .direction_change import filter_obsolete_direction_paths
from .file_classification import is_docs_path, is_test_path
from .task_scope import (
    extract_forbidden_repo_path_hints,
    extract_repo_path_hints,
    is_agent_internal_scope_path,
    split_normalized_repo_path_list,
)

MUTATING_TASK_SCOPE_WARNING = (
    "runnable or ambiguous task requires runnable estimated_files/write_scope; provide "
    "repo-relative file scope, explicit repo-relative path hints, or mark the task as "
    "analysis-only/report-only."
)
EXECUTION_UNREADY_SCOPE_WARNING = (
    "runnable or ambiguous task lacks runnable estimated_files/write_scope; include "
    "repo-relative file paths in the task, use /plan edit / planner flow to produce "
    "execution-ready scope, or mark the task as analysis-only/report-only."
)

_TASK_ACTION_WORD_RE = re.compile(r"[a-z][a-z0-9_-]*")
_MUTATING_TASK_ACTIONS = frozenset(
    {
        "add",
        "build",
        "change",
        "configure",
        "create",
        "document",
        "edit",
        "enable",
        "fix",
        "improve",
        "implement",
        "make",
        "migrate",
        "modify",
        "patch",
        "refactor",
        "remove",
        "rename",
        "repair",
        "support",
        "test",
        "touch",
        "update",
        "upgrade",
        "wire",
        "write",
    }
)
_WEAK_MUTATING_TASK_ACTIONS = frozenset({"change", "test"})
_NON_MUTATING_TASK_ACTIONS = frozenset(
    {
        "analyze",
        "analyse",
        "assess",
        "audit",
        "compare",
        "diagnose",
        "examine",
        "explain",
        "explore",
        "find",
        "inspect",
        "investigate",
        "identify",
        "locate",
        "map",
        "report",
        "review",
        "summarize",
        "summarise",
        "survey",
        "triage",
        "understand",
    }
)
_NON_MUTATING_TASK_PHRASES = (
    "analysis only",
    "explain the issue",
    "investigate and report",
    "no code changes",
    "no file changes",
    "read current",
    "read-only",
    "readonly",
    "report findings",
    "report only",
    "review and report",
    "summarize findings",
    "summarise findings",
)
_REPORT_ONLY_DOCUMENTATION_PHRASES = (
    "document findings",
    "document the findings",
    "findings documented",
    "findings are documented",
    "root cause documented",
    "root cause is documented",
    "summary documented",
    "summary is documented",
)
_REPORT_ONLY_DOCUMENTATION_MUTATING_TOKENS = frozenset({"document"})
_NOUN_PRONE_MUTATING_TASK_ACTIONS = frozenset(
    {
        "build",
        "change",
        "make",
        "patch",
        "repair",
        "support",
        "test",
    }
)
_TASK_ACTION_CLAUSE_CONNECTORS = frozenset(
    {
        "and",
        "must",
        "need",
        "needs",
        "require",
        "requires",
        "should",
        "then",
        "to",
        "will",
    }
)
_TASK_ACTION_TOKEN_CANONICAL_MAP = {
    "added": "add",
    "adding": "add",
    "adds": "add",
    "analysed": "analyse",
    "analyses": "analyse",
    "analysing": "analyse",
    "analyzed": "analyze",
    "analyzes": "analyze",
    "analyzing": "analyze",
    "assessed": "assess",
    "assesses": "assess",
    "assessing": "assess",
    "audited": "audit",
    "auditing": "audit",
    "audits": "audit",
    "changed": "change",
    "changes": "change",
    "changing": "change",
    "built": "build",
    "building": "build",
    "builds": "build",
    "compared": "compare",
    "compares": "compare",
    "comparing": "compare",
    "configured": "configure",
    "configures": "configure",
    "configuring": "configure",
    "created": "create",
    "creates": "create",
    "creating": "create",
    "documented": "document",
    "documenting": "document",
    "documents": "document",
    "diagnosed": "diagnose",
    "diagnoses": "diagnose",
    "diagnosing": "diagnose",
    "edited": "edit",
    "editing": "edit",
    "edits": "edit",
    "enabled": "enable",
    "enables": "enable",
    "enabling": "enable",
    "explained": "explain",
    "explaining": "explain",
    "explains": "explain",
    "examined": "examine",
    "examines": "examine",
    "examining": "examine",
    "explored": "explore",
    "explores": "explore",
    "exploring": "explore",
    "found": "find",
    "finding": "find",
    "finds": "find",
    "fixed": "fix",
    "fixes": "fix",
    "fixing": "fix",
    "identified": "identify",
    "identifies": "identify",
    "identifying": "identify",
    "improved": "improve",
    "improves": "improve",
    "improving": "improve",
    "implemented": "implement",
    "implementing": "implement",
    "implements": "implement",
    "inspected": "inspect",
    "inspecting": "inspect",
    "inspects": "inspect",
    "investigated": "investigate",
    "investigates": "investigate",
    "investigating": "investigate",
    "located": "locate",
    "locates": "locate",
    "locating": "locate",
    "made": "make",
    "makes": "make",
    "making": "make",
    "mapped": "map",
    "mapping": "map",
    "maps": "map",
    "migrated": "migrate",
    "migrates": "migrate",
    "migrating": "migrate",
    "modified": "modify",
    "modifies": "modify",
    "modifying": "modify",
    "patched": "patch",
    "patches": "patch",
    "patching": "patch",
    "refactored": "refactor",
    "refactoring": "refactor",
    "refactors": "refactor",
    "removed": "remove",
    "removes": "remove",
    "removing": "remove",
    "renamed": "rename",
    "renames": "rename",
    "renaming": "rename",
    "repaired": "repair",
    "repairing": "repair",
    "repairs": "repair",
    "reported": "report",
    "reporting": "report",
    "reports": "report",
    "read": "read",
    "reading": "read",
    "reads": "read",
    "reviewed": "review",
    "reviewing": "review",
    "reviews": "review",
    "surveyed": "survey",
    "surveying": "survey",
    "surveys": "survey",
    "summarised": "summarise",
    "summarises": "summarise",
    "summarising": "summarise",
    "summarized": "summarize",
    "summarizes": "summarize",
    "summarizing": "summarize",
    "supported": "support",
    "supporting": "support",
    "supports": "support",
    "tested": "test",
    "testing": "test",
    "tests": "test",
    "touched": "touch",
    "touches": "touch",
    "touching": "touch",
    "triaged": "triage",
    "triages": "triage",
    "triaging": "triage",
    "understood": "understand",
    "understanding": "understand",
    "understands": "understand",
    "updated": "update",
    "updates": "update",
    "updating": "update",
    "upgraded": "upgrade",
    "upgrades": "upgrade",
    "upgrading": "upgrade",
    "wired": "wire",
    "wires": "wire",
    "wiring": "wire",
    "writes": "write",
    "writing": "write",
    "written": "write",
    "wrote": "write",
}

TASK_KIND_ANALYSIS_ONLY = "analysis_only"
TASK_KIND_IMPLEMENTATION = "implementation"
TASK_KIND_TEST_ONLY = "test_only"
TASK_KIND_VERIFICATION_ONLY = "verification_only"
TASK_KIND_DOCUMENTATION_ONLY = "documentation_only"
TASK_KIND_UNKNOWN = "unknown"

ZERO_DIFF_ANALYSIS_OK = "analysis_artifact"
ZERO_DIFF_REQUIRES_VERIFICATION = "requires_verification"
ZERO_DIFF_SUSPICIOUS = "needs_attention"

_VERIFICATION_ONLY_RE = re.compile(
    r"^\s*(?:run\s+)?(?:verify|verification|validate|validation|check|tests?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskFileScopeNormalization:
    estimated_files: list[str]
    write_scope: list[str]
    warnings: list[str]
    requires_runnable_scope: bool
    task_kind: str = TASK_KIND_UNKNOWN
    task_kind_reason: str = "unclassified"
    zero_diff_policy: str = ZERO_DIFF_SUSPICIOUS


@dataclass(frozen=True)
class ExecutionUnreadyTask:
    task_id: str
    title: str
    status: str
    warning: str


@dataclass(frozen=True)
class TaskLifecycleClassification:
    kind: str
    reason_code: str
    mutating: bool
    requires_runnable_scope: bool
    zero_diff_policy: str

    @property
    def read_only(self) -> bool:
        return not self.mutating


def normalized_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _scope_identity_key(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").rstrip("/")
    if normalized.casefold() in {"readme", "readme.md"}:
        return "__readme_alias__"
    return normalized.casefold()


def task_text_blob(*, title: str, description: str, acceptance_criteria: list[str]) -> str:
    return "\n".join(
        [
            str(title or ""),
            str(description or ""),
            *(str(item or "") for item in acceptance_criteria),
        ]
    ).strip()


def _first_task_action_word(text: str) -> str:
    for token in _TASK_ACTION_WORD_RE.findall((text or "").casefold()):
        if token in {"a", "an", "the", "to"}:
            continue
        return _normalize_task_action_token(token)
    return ""


def _normalize_task_action_token(token: str) -> str:
    return _TASK_ACTION_TOKEN_CANONICAL_MAP.get(token.casefold(), token.casefold())


def _normalized_task_action_tokens(text: str) -> list[str]:
    return [
        _normalize_task_action_token(token)
        for token in _TASK_ACTION_WORD_RE.findall((text or "").casefold())
    ]


def contains_mutating_task_signal(text: str) -> bool:
    return any(token in _MUTATING_TASK_ACTIONS for token in _normalized_task_action_tokens(text))


def has_mutating_task_action_clause(text: str) -> bool:
    return _has_mutating_task_action_clause(text, allowed_tokens=frozenset())


def _has_mutating_task_action_clause(text: str, *, allowed_tokens: frozenset[str]) -> bool:
    folded = (text or "").casefold()
    previous_token = ""
    previous_end = 0
    for index, match in enumerate(_TASK_ACTION_WORD_RE.finditer(folded)):
        token = _normalize_task_action_token(match.group(0))
        gap = folded[previous_end : match.start()]
        starts_clause = (
            index == 0
            or any(separator in gap for separator in "\n.;:")
            or ("," in gap and token not in _NOUN_PRONE_MUTATING_TASK_ACTIONS)
            or previous_token in _TASK_ACTION_CLAUSE_CONNECTORS
        )
        if starts_clause and token in (
            _MUTATING_TASK_ACTIONS - _WEAK_MUTATING_TASK_ACTIONS - allowed_tokens
        ):
            return True
        previous_token = token
        previous_end = match.end()
    return False


def is_clearly_non_mutating_task(
    *,
    title: str,
    description: str,
    acceptance_criteria: list[str],
) -> bool:
    raw_text = task_text_blob(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
    )
    text = raw_text.casefold()
    if not text:
        return False
    normalized_action_tokens = _normalized_task_action_tokens(text)
    first_title_word = _first_task_action_word(title)
    has_explicit_non_mutating_phrase = any(phrase in text for phrase in _NON_MUTATING_TASK_PHRASES)
    has_report_only_documentation_phrase = any(
        phrase in text for phrase in _REPORT_ONLY_DOCUMENTATION_PHRASES
    )
    allowed_report_only_tokens = (
        _REPORT_ONLY_DOCUMENTATION_MUTATING_TOKENS
        if has_report_only_documentation_phrase
        else frozenset()
    )
    has_mutating_action_clause = _has_mutating_task_action_clause(
        raw_text,
        allowed_tokens=allowed_report_only_tokens,
    )
    if (
        first_title_word in _NON_MUTATING_TASK_ACTIONS
        and has_report_only_documentation_phrase
        and not has_mutating_action_clause
    ):
        return True
    if has_explicit_non_mutating_phrase and not has_mutating_action_clause:
        return True
    if first_title_word == "read" and not has_mutating_action_clause:
        return True
    if first_title_word in _NON_MUTATING_TASK_ACTIONS:
        if not has_mutating_action_clause:
            return True
    if any(token in _MUTATING_TASK_ACTIONS for token in normalized_action_tokens):
        return False
    if any(token in _NON_MUTATING_TASK_ACTIONS for token in normalized_action_tokens):
        return not has_mutating_action_clause
    if first_title_word in _NON_MUTATING_TASK_ACTIONS:
        return True
    return has_explicit_non_mutating_phrase


def _normalized_scoped_paths(*, estimated_files: list[str], write_scope: list[str]) -> list[str]:
    return _dedupe_keep_order([*estimated_files, *write_scope])


def _coerce_explicit_task_kind(value: Any) -> str | None:
    raw = str(value or "").strip().casefold().replace("-", "_")
    if not raw:
        return None
    aliases = {
        "analysis": TASK_KIND_ANALYSIS_ONLY,
        "analysis_only": TASK_KIND_ANALYSIS_ONLY,
        "discovery": TASK_KIND_ANALYSIS_ONLY,
        "read_only": TASK_KIND_ANALYSIS_ONLY,
        "readonly": TASK_KIND_ANALYSIS_ONLY,
        "report_only": TASK_KIND_ANALYSIS_ONLY,
        "implementation": TASK_KIND_IMPLEMENTATION,
        "mutation": TASK_KIND_IMPLEMENTATION,
        "mutating": TASK_KIND_IMPLEMENTATION,
        "test": TASK_KIND_TEST_ONLY,
        "test_only": TASK_KIND_TEST_ONLY,
        "verification": TASK_KIND_VERIFICATION_ONLY,
        "verification_only": TASK_KIND_VERIFICATION_ONLY,
        "docs": TASK_KIND_DOCUMENTATION_ONLY,
        "documentation": TASK_KIND_DOCUMENTATION_ONLY,
        "documentation_only": TASK_KIND_DOCUMENTATION_ONLY,
        "unknown": TASK_KIND_UNKNOWN,
    }
    return aliases.get(raw)


def _classification_for_kind(kind: str, *, reason_code: str) -> TaskLifecycleClassification:
    if kind == TASK_KIND_ANALYSIS_ONLY:
        return TaskLifecycleClassification(
            kind=kind,
            reason_code=reason_code,
            mutating=False,
            requires_runnable_scope=False,
            zero_diff_policy=ZERO_DIFF_ANALYSIS_OK,
        )
    if kind == TASK_KIND_VERIFICATION_ONLY:
        return TaskLifecycleClassification(
            kind=kind,
            reason_code=reason_code,
            mutating=False,
            requires_runnable_scope=False,
            zero_diff_policy=ZERO_DIFF_REQUIRES_VERIFICATION,
        )
    if kind == TASK_KIND_TEST_ONLY:
        return TaskLifecycleClassification(
            kind=kind,
            reason_code=reason_code,
            mutating=True,
            requires_runnable_scope=True,
            zero_diff_policy=ZERO_DIFF_SUSPICIOUS,
        )
    if kind == TASK_KIND_DOCUMENTATION_ONLY:
        return TaskLifecycleClassification(
            kind=kind,
            reason_code=reason_code,
            mutating=True,
            requires_runnable_scope=True,
            zero_diff_policy=ZERO_DIFF_SUSPICIOUS,
        )
    if kind == TASK_KIND_IMPLEMENTATION:
        return TaskLifecycleClassification(
            kind=kind,
            reason_code=reason_code,
            mutating=True,
            requires_runnable_scope=True,
            zero_diff_policy=ZERO_DIFF_REQUIRES_VERIFICATION,
        )
    return TaskLifecycleClassification(
        kind=TASK_KIND_UNKNOWN,
        reason_code=reason_code,
        mutating=True,
        requires_runnable_scope=True,
        zero_diff_policy=ZERO_DIFF_SUSPICIOUS,
    )


def classify_task_lifecycle(
    *,
    title: str,
    description: str,
    acceptance_criteria: list[str],
    estimated_files: list[str] | None = None,
    write_scope: list[str] | None = None,
    explicit_analysis_only: bool | None = None,
    explicit_task_kind: Any = None,
) -> TaskLifecycleClassification:
    explicit_kind = _coerce_explicit_task_kind(explicit_task_kind)
    if explicit_analysis_only is True:
        return _classification_for_kind(
            TASK_KIND_ANALYSIS_ONLY,
            reason_code="explicit_analysis_only",
        )
    if explicit_kind is not None:
        return _classification_for_kind(
            explicit_kind,
            reason_code=f"explicit_task_kind:{explicit_kind}",
        )

    acceptance = normalized_text_list(acceptance_criteria)
    if is_clearly_non_mutating_task(
        title=title,
        description=description,
        acceptance_criteria=acceptance,
    ):
        return _classification_for_kind(
            TASK_KIND_ANALYSIS_ONLY,
            reason_code="non_mutating_intent",
        )

    text = task_text_blob(
        title=title,
        description=description,
        acceptance_criteria=acceptance,
    )
    has_mutating_clause = _has_mutating_task_action_clause(text, allowed_tokens=frozenset())
    if _VERIFICATION_ONLY_RE.search(text) and not has_mutating_clause:
        return _classification_for_kind(
            TASK_KIND_VERIFICATION_ONLY,
            reason_code="verification_intent",
        )

    scoped_paths = _normalized_scoped_paths(
        estimated_files=list(estimated_files or []),
        write_scope=list(write_scope or []),
    )
    if scoped_paths and all(is_test_path(path) for path in scoped_paths):
        return _classification_for_kind(TASK_KIND_TEST_ONLY, reason_code="test_scope")
    if scoped_paths and all(is_docs_path(path) for path in scoped_paths):
        return _classification_for_kind(
            TASK_KIND_DOCUMENTATION_ONLY,
            reason_code="documentation_scope",
        )
    if scoped_paths:
        return _classification_for_kind(
            TASK_KIND_IMPLEMENTATION,
            reason_code="runnable_file_scope",
        )
    if contains_mutating_task_signal(text):
        return _classification_for_kind(
            TASK_KIND_IMPLEMENTATION,
            reason_code="mutating_intent",
        )
    return _classification_for_kind(TASK_KIND_UNKNOWN, reason_code="ambiguous_intent")


def has_runnable_local_file_scope(
    *,
    estimated_files: list[str],
    write_scope: list[str],
) -> bool:
    return bool(estimated_files or write_scope)


def normalize_existing_task_scope_fields(
    *,
    estimated_files: Any,
    write_scope: Any,
) -> tuple[list[str], list[str]]:
    normalized_estimated, _dropped_estimated = split_normalized_repo_path_list(estimated_files)
    normalized_write_scope, _dropped_write_scope = split_normalized_repo_path_list(write_scope)

    def _filter_runnable_paths(paths: list[str]) -> list[str]:
        filtered: list[str] = []
        for path in paths:
            if is_agent_internal_scope_path(path):
                continue
            filtered.append(path)
        return _dedupe_keep_order(filtered)

    return _filter_runnable_paths(normalized_estimated), _filter_runnable_paths(
        normalized_write_scope
    )


_normalize_existing_task_scope_fields = normalize_existing_task_scope_fields


def task_requires_runnable_file_scope(
    *,
    title: str,
    description: str,
    acceptance_criteria: list[str],
    estimated_files: list[str],
    write_scope: list[str],
) -> bool:
    lifecycle = classify_task_lifecycle(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        estimated_files=estimated_files,
        write_scope=write_scope,
    )
    if lifecycle.requires_runnable_scope:
        return True
    text = task_text_blob(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
    )
    if extract_repo_path_hints(text):
        return lifecycle.mutating
    return lifecycle.requires_runnable_scope


def normalize_task_file_fields(
    *,
    title: str,
    description: str,
    acceptance_criteria: list[str],
    estimated_files: Any,
    write_scope: Any,
    warning_prefix: str,
    latest_user_text: str = "",
) -> TaskFileScopeNormalization:
    warnings: list[str] = []
    normalized_estimated, dropped_estimated = split_normalized_repo_path_list(estimated_files)
    normalized_write_scope, dropped_write_scope = split_normalized_repo_path_list(write_scope)
    if dropped_estimated:
        warnings.append(
            f"{warning_prefix}: dropped invalid estimated_files entries: "
            + ", ".join(dropped_estimated)
        )
    if dropped_write_scope:
        warnings.append(
            f"{warning_prefix}: dropped invalid write_scope entries: "
            + ", ".join(dropped_write_scope)
        )

    task_text = task_text_blob(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
    )
    initial_lifecycle = classify_task_lifecycle(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        estimated_files=normalized_estimated,
        write_scope=normalized_write_scope,
    )
    clearly_non_mutating = initial_lifecycle.kind == TASK_KIND_ANALYSIS_ONLY
    forbidden_paths = extract_forbidden_repo_path_hints(
        "\n".join([task_text, str(latest_user_text or "")])
    )
    forbidden_identities = {_scope_identity_key(path) for path in forbidden_paths}

    def _drop_forbidden(paths: list[str]) -> tuple[list[str], list[str]]:
        kept: list[str] = []
        dropped: list[str] = []
        for path in paths:
            if _scope_identity_key(path) in forbidden_identities:
                dropped.append(path)
                continue
            kept.append(path)
        return kept, dropped

    inferred_paths = [] if clearly_non_mutating else extract_repo_path_hints(task_text)
    inferred_paths, forbidden_inferred_paths = _drop_forbidden(inferred_paths)
    if forbidden_inferred_paths:
        warnings.append(
            f"{warning_prefix}: ignored forbidden path hints from task text: "
            + ", ".join(forbidden_inferred_paths)
        )
    inferred_paths, obsolete_inferred_paths = filter_obsolete_direction_paths(
        inferred_paths,
        latest_user_text=latest_user_text,
        task_text=task_text,
    )
    if obsolete_inferred_paths:
        warnings.append(
            f"{warning_prefix}: ignored obsolete inferred path hints from direction-change text: "
            + ", ".join(obsolete_inferred_paths)
        )
    added_inferred: list[str] = []
    for path in inferred_paths:
        if path not in normalized_estimated:
            normalized_estimated.append(path)
            added_inferred.append(path)
    if added_inferred:
        warnings.append(
            f"{warning_prefix}: inferred estimated_files from task text: "
            + ", ".join(added_inferred)
        )

    filtered_estimated: list[str] = []
    dropped_internal_estimated: list[str] = []
    for path in normalized_estimated:
        if is_agent_internal_scope_path(path):
            dropped_internal_estimated.append(path)
            continue
        filtered_estimated.append(path)
    if dropped_internal_estimated:
        warnings.append(
            f"{warning_prefix}: dropped protected estimated_files entries: "
            + ", ".join(dropped_internal_estimated)
        )
    filtered_estimated, dropped_forbidden_estimated = _drop_forbidden(filtered_estimated)
    if dropped_forbidden_estimated:
        warnings.append(
            f"{warning_prefix}: dropped forbidden estimated_files entries: "
            + ", ".join(dropped_forbidden_estimated)
        )

    filtered_write_scope: list[str] = []
    dropped_internal_write_scope: list[str] = []
    for path in normalized_write_scope:
        if is_agent_internal_scope_path(path):
            dropped_internal_write_scope.append(path)
            continue
        filtered_write_scope.append(path)
    if dropped_internal_write_scope:
        warnings.append(
            f"{warning_prefix}: dropped protected write_scope entries: "
            + ", ".join(dropped_internal_write_scope)
        )
    filtered_write_scope, dropped_forbidden_write_scope = _drop_forbidden(filtered_write_scope)
    if dropped_forbidden_write_scope:
        warnings.append(
            f"{warning_prefix}: dropped forbidden write_scope entries: "
            + ", ".join(dropped_forbidden_write_scope)
        )

    filtered_estimated = _dedupe_keep_order(filtered_estimated)
    filtered_write_scope = _dedupe_keep_order(filtered_write_scope)
    lifecycle = classify_task_lifecycle(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        estimated_files=filtered_estimated,
        write_scope=filtered_write_scope,
    )
    if lifecycle.kind == TASK_KIND_ANALYSIS_ONLY:
        if filtered_estimated or filtered_write_scope:
            warnings.append(
                f"{warning_prefix}: cleared file mutation scope for analysis-only/report-only task"
            )
        return TaskFileScopeNormalization(
            estimated_files=[],
            write_scope=[],
            warnings=warnings,
            requires_runnable_scope=False,
            task_kind=lifecycle.kind,
            task_kind_reason=lifecycle.reason_code,
            zero_diff_policy=lifecycle.zero_diff_policy,
        )
    expanded_write_scope: list[str] = list(filtered_write_scope)
    added_to_write_scope: list[str] = []
    for path in filtered_estimated:
        if path in expanded_write_scope:
            continue
        expanded_write_scope.append(path)
        added_to_write_scope.append(path)
    if added_to_write_scope:
        warnings.append(
            f"{warning_prefix}: expanded write_scope to include estimated_files: "
            + ", ".join(added_to_write_scope)
        )
    requires_runnable_scope = task_requires_runnable_file_scope(
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        estimated_files=filtered_estimated,
        write_scope=expanded_write_scope,
    )
    if requires_runnable_scope and not has_runnable_local_file_scope(
        estimated_files=filtered_estimated,
        write_scope=expanded_write_scope,
    ):
        warnings.append(f"{warning_prefix}: {MUTATING_TASK_SCOPE_WARNING}")
    return TaskFileScopeNormalization(
        estimated_files=filtered_estimated,
        write_scope=expanded_write_scope,
        warnings=warnings,
        requires_runnable_scope=requires_runnable_scope,
        task_kind=lifecycle.kind,
        task_kind_reason=lifecycle.reason_code,
        zero_diff_policy=lifecycle.zero_diff_policy,
    )


def task_readiness_warning(*, task_id: str, title: str = "") -> str:
    label = str(task_id or "").strip() or "-"
    title_text = str(title or "").strip()
    if title_text:
        return f"Task {label} ({title_text}): {EXECUTION_UNREADY_SCOPE_WARNING}"
    return f"Task {label}: {EXECUTION_UNREADY_SCOPE_WARNING}"


def manual_task_scope_error_message(*, title: str) -> str:
    title_text = str(title or "").strip()
    if title_text:
        return (
            f"Task '{title_text}' looks like runnable or ambiguous work but lacks runnable "
            "file scope. Include repo-relative file paths in /task, use /plan edit / the "
            "planner flow to produce execution-ready scope, or mark the task as "
            "analysis-only/report-only if no file mutation is intended."
        )
    return (
        "Task looks like runnable or ambiguous work but lacks runnable file scope. Include "
        "repo-relative file paths in /task, use /plan edit / the planner flow to produce "
        "execution-ready scope, or mark the task as analysis-only/report-only if no file "
        "mutation is intended."
    )


def _canonical_task_status(status: str) -> str:
    value = (status or "").strip().lower()
    if value == "todo":
        return "planned"
    return value or "planned"


def _parse_only_ids(only: str | None) -> set[str] | None:
    if only is None:
        return None
    ids = {part.strip() for part in only.split(",") if part.strip()}
    return ids or None


def status_is_execution_candidate(
    status: str,
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
    retry_merge_conflicts: bool,
) -> bool:
    if status in {"planned", "ready_for_merge"}:
        return True
    if status in {"failed", "verify_failed", "candidate_rejected"} and retry_failed:
        return True
    if status == "changes_requested" and retry_changes_requested:
        return True
    if status == "merge_conflict" and retry_merge_conflicts:
        return True
    return False


_status_is_execution_candidate = status_is_execution_candidate


def task_is_missing_runnable_scope(task: dict[str, Any]) -> bool:
    estimated_files, write_scope = _normalize_existing_task_scope_fields(
        estimated_files=task.get("estimated_files"),
        write_scope=task.get("write_scope"),
    )
    return task_requires_runnable_file_scope(
        title=str(task.get("title") or "").strip(),
        description=str(task.get("description") or "").strip(),
        acceptance_criteria=normalized_text_list(task.get("acceptance_criteria")),
        estimated_files=estimated_files,
        write_scope=write_scope,
    ) and not has_runnable_local_file_scope(
        estimated_files=estimated_files,
        write_scope=write_scope,
    )


def find_execution_unready_mutating_tasks(
    plan: dict[str, Any],
    *,
    retry_failed: bool = False,
    retry_changes_requested: bool = False,
    retry_merge_conflicts: bool = False,
    only: str | None = None,
) -> list[ExecutionUnreadyTask]:
    only_ids = _parse_only_ids(only)
    tasks_raw = plan.get("tasks")
    if not isinstance(tasks_raw, list):
        return []

    issues: list[ExecutionUnreadyTask] = []
    for index, task in enumerate(tasks_raw):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip() or f"task[{index}]"
        status = _canonical_task_status(str(task.get("status") or ""))
        # Merge-phase tasks are handled before scheduler filtering, so mirror run_swarm's
        # execution surface instead of treating --only as a complete preflight filter.
        if (
            only_ids is not None
            and task_id not in only_ids
            and status != "ready_for_merge"
            and not (retry_merge_conflicts and status == "merge_conflict")
        ):
            continue
        if not status_is_execution_candidate(
            status,
            retry_failed=retry_failed,
            retry_changes_requested=retry_changes_requested,
            retry_merge_conflicts=retry_merge_conflicts,
        ):
            continue
        title = str(task.get("title") or "").strip()
        if not task_is_missing_runnable_scope(task):
            continue
        issues.append(
            ExecutionUnreadyTask(
                task_id=task_id,
                title=title,
                status=status,
                warning=task_readiness_warning(task_id=task_id, title=title),
            )
        )
    return issues


def format_execution_readiness_block(issues: list[ExecutionUnreadyTask]) -> str:
    if not issues:
        return ""
    preview = "; ".join(issue.warning for issue in issues[:5])
    if len(issues) > 5:
        preview += f"; +{len(issues) - 5} more"
    return "Execution blocked: " + preview
