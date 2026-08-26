from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .file_classification import is_code_implementation_path, is_test_path
from .task_dependencies import infer_ordered_predecessor_dependency
from .task_scope import (
    is_explicit_repo_path_pattern,
    normalize_claimed_scope_patterns,
    normalize_repo_path_list,
)
from .verification_repair import TASK_STATUS_COMPLETED_UNVERIFIED

_EXPECTED_RED_REGRESSION_TEXT_RE = re.compile(
    r"\b(?:red|known[- ]?failing|expected\s+to\s+fail|fail(?:s|ing)?\s+before\s+fix|"
    r"regression)\b.{0,120}\btests?\b"
    r"|\btests?\b.{0,120}\b(?:red|known[- ]?failing|expected\s+to\s+fail|"
    r"fail(?:s|ing)?\s+before\s+fix|regression)\b",
    re.IGNORECASE | re.DOTALL,
)
_SUPPORT_SCOPE_DIRS = frozenset(
    {
        ".github",
        ".alysis",
        "__tests__",
        "doc",
        "docs",
        "example",
        "examples",
        "fixture",
        "fixtures",
        "note",
        "notes",
        "spec",
        "specs",
        "test",
        "tests",
    }
)
# ``completed_unverified`` belongs here: the task's work landed and was kept, only
# the authoritative check for it was missing. Treating it as unfinished would block
# every dependent task for a reason that has nothing to do with the work itself.
SUCCESSFUL_TERMINAL_STATUSES = frozenset(
    {"done", "already_satisfied", TASK_STATUS_COMPLETED_UNVERIFIED}
)


def canonical_task_status(status: str) -> str:
    value = (status or "").strip().lower()
    if value == "todo":
        return "planned"
    return value or "planned"


@dataclass(frozen=True)
class TaskCandidate:
    task_id: str
    title: str
    status: str
    dependencies: tuple[str, ...]
    estimated_files: frozenset[str]
    write_scope: frozenset[str]
    claimed_patterns: tuple[str, ...]
    claimed_files: frozenset[str]
    ambiguous_patterns: tuple[str, ...]
    parallel_group: str
    attempts: int
    task: dict[str, Any]

    @property
    def has_estimated_files(self) -> bool:
        return bool(self.estimated_files)

    @property
    def has_claimed_scope(self) -> bool:
        return bool(self.claimed_patterns)

    @property
    def has_precise_claimed_scope(self) -> bool:
        return bool(self.claimed_files) and not self.ambiguous_patterns

    def claimed_scope_reason(self) -> str:
        if not self.claimed_patterns:
            return "missing claimed write scope metadata (estimated_files/write_scope)"
        if self.ambiguous_patterns:
            return f"ambiguous claimed scope: {_preview_items(self.ambiguous_patterns)}"
        return f"precise claimed scope: {_preview_items(sorted(self.claimed_files))}"


@dataclass
class Batch:
    index: int
    task_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class Schedule:
    base_branch: str
    runnable: list[TaskCandidate]
    ready_for_merge: list[TaskCandidate]
    skipped: dict[str, str]
    batches: list[Batch]


def _normalize_estimated_files(task: dict[str, Any]) -> frozenset[str]:
    return frozenset(normalize_repo_path_list(task.get("estimated_files")))


def _normalize_write_scope(task: dict[str, Any]) -> frozenset[str]:
    return frozenset(normalize_repo_path_list(task.get("write_scope")))


def _normalize_claimed_patterns(
    task: dict[str, Any],
) -> tuple[tuple[str, ...], frozenset[str], tuple[str, ...]]:
    claimed = tuple(normalize_claimed_scope_patterns(task))
    precise = frozenset(item for item in claimed if is_explicit_repo_path_pattern(item))
    ambiguous = tuple(item for item in claimed if item not in precise)
    return claimed, precise, ambiguous


def _preview_items(items: list[str] | tuple[str, ...], *, limit: int = 3) -> str:
    values = list(items)
    if not values:
        return "(none)"
    if len(values) <= limit:
        return ", ".join(values)
    hidden = len(values) - limit
    return ", ".join(values[:limit]) + f", +{hidden} more"


def _task_text(task: dict[str, Any]) -> str:
    acceptance = task.get("acceptance_criteria") or []
    if not isinstance(acceptance, list):
        acceptance = []
    return " ".join(
        part
        for part in [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            *(str(item or "") for item in acceptance),
        ]
        if part
    )


def _is_test_or_support_scope_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return False
    if normalized.endswith("/**"):
        normalized = normalized[:-3].strip("/")
    return is_test_path(normalized)


def _is_primary_implementation_scope_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return False
    if _is_test_or_support_scope_path(normalized):
        return False
    if normalized.endswith("/**"):
        parts = [part.casefold() for part in normalized[:-3].strip("/").split("/") if part]
        return bool(parts) and not any(
            part.startswith(".") or part in _SUPPORT_SCOPE_DIRS for part in parts
        )
    name = PurePosixPath(normalized).name.casefold()
    if name in {"readme", "readme.md"} or name.endswith((".md", ".rst", ".txt")):
        return False
    if name in {
        "cargo.toml",
        "go.mod",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
    }:
        return True
    return is_code_implementation_path(normalized)


def _task_has_primary_implementation_scope(task: dict[str, Any]) -> bool:
    paths = [
        *normalize_repo_path_list(task.get("estimated_files")),
        *normalize_repo_path_list(task.get("write_scope")),
    ]
    return any(_is_primary_implementation_scope_path(path) for path in paths)


def is_expected_red_regression_precursor(task: dict[str, Any]) -> bool:
    text = _task_text(task)
    if not _EXPECTED_RED_REGRESSION_TEXT_RE.search(text):
        return False
    paths = [
        *normalize_repo_path_list(task.get("estimated_files")),
        *normalize_repo_path_list(task.get("write_scope")),
    ]
    return bool(paths) and all(_is_test_or_support_scope_path(path) for path in paths)


def _effective_dependencies(
    *,
    task: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> tuple[str, ...]:
    deps_raw = task.get("dependencies") or []
    deps = [str(x).strip() for x in deps_raw if str(x).strip()]
    if not deps:
        inferred = infer_ordered_predecessor_dependency(tasks=tasks, task=task)
        if inferred is not None and inferred.depends_on not in deps:
            deps.append(inferred.depends_on)
    return tuple(deps)


def _to_candidate(task: dict[str, Any], *, tasks: list[dict[str, Any]]) -> TaskCandidate:
    task_id = str(task.get("id") or "").strip()
    title = str(task.get("title") or "").strip()
    status = canonical_task_status(str(task.get("status") or ""))
    deps = _effective_dependencies(task=task, tasks=tasks)
    parallel_group = str(task.get("parallel_group") or "").strip()
    attempts_raw = task.get("attempts")
    try:
        attempts = int(attempts_raw) if attempts_raw is not None else 0
    except (TypeError, ValueError):
        attempts = 0
    if attempts < 0:
        attempts = 0
    estimated_files = _normalize_estimated_files(task)
    write_scope = _normalize_write_scope(task)
    claimed_patterns, claimed_files, ambiguous_patterns = _normalize_claimed_patterns(task)
    return TaskCandidate(
        task_id=task_id,
        title=title,
        status=status,
        dependencies=deps,
        estimated_files=estimated_files,
        write_scope=write_scope,
        claimed_patterns=claimed_patterns,
        claimed_files=claimed_files,
        ambiguous_patterns=ambiguous_patterns,
        parallel_group=parallel_group,
        attempts=attempts,
        task=task,
    )


def _runnable_status(
    status: str,
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
) -> bool:
    if status in {"planned", "interrupted", "cancelled"}:
        return True
    if status in {"failed", "verify_failed", "candidate_rejected"} and retry_failed:
        return True
    if status == "changes_requested" and retry_changes_requested:
        return True
    return False


def _deps_done(
    candidate: TaskCandidate,
    *,
    task_by_id: dict[str, TaskCandidate],
) -> tuple[bool, str]:
    for dep_id in candidate.dependencies:
        dep = task_by_id.get(dep_id)
        if dep is None:
            return False, f"dependency missing: {dep_id}"
        if (
            dep.status == "ready_for_merge"
            and is_expected_red_regression_precursor(dep.task)
            and _task_has_primary_implementation_scope(candidate.task)
        ):
            continue
        if dep.status not in SUCCESSFUL_TERMINAL_STATUSES:
            return False, f"dependency not done: {dep_id} ({dep.status})"
    return True, ""


def select_task_candidates(
    *,
    tasks: list[dict[str, Any]],
    retry_failed: bool,
    retry_changes_requested: bool,
    max_attempts: int | None = None,
    only_ids: set[str] | None = None,
) -> tuple[list[TaskCandidate], list[TaskCandidate], dict[str, str]]:
    candidates = [_to_candidate(task, tasks=tasks) for task in tasks]
    task_by_id = {c.task_id: c for c in candidates}

    runnable: list[TaskCandidate] = []
    ready_for_merge: list[TaskCandidate] = []
    skipped: dict[str, str] = {}

    for candidate in candidates:
        if not candidate.task_id:
            continue

        if only_ids is not None and candidate.task_id not in only_ids:
            skipped[candidate.task_id] = "filtered by --only"
            continue

        if candidate.status in SUCCESSFUL_TERMINAL_STATUSES:
            skipped[candidate.task_id] = "already done"
            continue

        if candidate.status == "ready_for_merge":
            ready_for_merge.append(candidate)
            continue

        if not _runnable_status(
            candidate.status,
            retry_failed=retry_failed,
            retry_changes_requested=retry_changes_requested,
        ):
            skipped[candidate.task_id] = f"status not runnable: {candidate.status}"
            continue

        if max_attempts is not None and candidate.attempts >= max_attempts:
            skipped[candidate.task_id] = (
                f"attempt limit reached: {candidate.attempts} >= {max_attempts}"
            )
            continue

        ok, reason = _deps_done(candidate, task_by_id=task_by_id)
        if not ok:
            skipped[candidate.task_id] = reason
            continue

        runnable.append(candidate)

    return runnable, ready_for_merge, skipped


def _pair_safe(a: TaskCandidate, b: TaskCandidate) -> tuple[bool, str]:
    if not a.has_precise_claimed_scope:
        return False, a.claimed_scope_reason()
    if not b.has_precise_claimed_scope:
        return False, b.claimed_scope_reason()
    overlap = a.claimed_files & b.claimed_files
    if overlap:
        return False, f"claimed scope overlap: {_preview_items(sorted(overlap))}"
    if a.parallel_group and b.parallel_group and a.parallel_group == b.parallel_group:
        return False, f"same parallel_group: {a.parallel_group}"
    return True, "claimed scopes disjoint"


def build_batches(
    *,
    runnable: list[TaskCandidate],
    parallel: int,
) -> list[Batch]:
    if parallel <= 0:
        parallel = 1
    ordered = sorted(
        runnable,
        key=lambda c: (
            not c.has_precise_claimed_scope,
            c.task_id,
        ),
    )
    runnable_by_id = {candidate.task_id: candidate for candidate in runnable}

    batches: list[Batch] = []
    for candidate in ordered:
        placed = False
        blockers: list[str] = []
        if candidate.has_precise_claimed_scope:
            for batch in batches:
                if len(batch.task_ids) >= parallel:
                    continue
                all_safe = True
                reasons: list[str] = []
                for existing_id in batch.task_ids:
                    existing = runnable_by_id[existing_id]
                    safe, reason = _pair_safe(candidate, existing)
                    if not safe:
                        all_safe = False
                        blockers.append(f"blocked by {existing.task_id}: {reason}")
                        break
                    reasons.append(f"{existing.task_id}: {reason}")
                if all_safe:
                    batch.task_ids.append(candidate.task_id)
                    batch.reasons.append(f"{candidate.task_id} joined batch: {', '.join(reasons)}")
                    placed = True
                    break

        if not placed:
            idx = len(batches) + 1
            batch = Batch(index=idx, task_ids=[candidate.task_id])
            if not candidate.has_precise_claimed_scope:
                batch.reasons.append(
                    f"{candidate.task_id} runs alone: {candidate.claimed_scope_reason()}"
                )
            elif blockers:
                batch.reasons.append(
                    f"{candidate.task_id} starts new batch: {_preview_items(blockers, limit=2)}"
                )
            else:
                batch.reasons.append(
                    f"{candidate.task_id} starts new batch: {candidate.claimed_scope_reason()}"
                )
            batches.append(batch)

    return batches


def compute_schedule(
    *,
    base_branch: str,
    tasks: list[dict[str, Any]],
    parallel: int,
    max_tasks: int | None,
    retry_failed: bool,
    retry_changes_requested: bool = False,
    max_attempts: int | None = None,
    only_ids: set[str] | None = None,
) -> Schedule:
    runnable, ready_for_merge, skipped = select_task_candidates(
        tasks=tasks,
        retry_failed=retry_failed,
        retry_changes_requested=retry_changes_requested,
        max_attempts=max_attempts,
        only_ids=only_ids,
    )
    if max_tasks is not None and max_tasks > 0:
        runnable = runnable[:max_tasks]
    batches = build_batches(runnable=runnable, parallel=parallel)
    return Schedule(
        base_branch=base_branch,
        runnable=runnable,
        ready_for_merge=ready_for_merge,
        skipped=skipped,
        batches=batches,
    )
