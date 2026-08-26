from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .task_readiness import is_clearly_non_mutating_task, normalized_text_list

_ORDERING_SIGNAL_RE = re.compile(
    r"\b(?:after|based\s+on|depend(?:s|ed|ing)?\s+on|follow[- ]?up|following|"
    r"next\s+(?:task|step|change|item|phase|work)|once|previous|prior|subsequent|then)\b",
    re.IGNORECASE,
)
_EXPLICIT_PREVIOUS_RE = re.compile(
    r"\b(?:after\s+that|after\s+the\s+previous|after\s+the\s+prior|follow[- ]?up|"
    r"following\s+that|next\s+(?:task|step|change|item|phase|work)|"
    r"previous\s+task|prior\s+task|then)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*", re.IGNORECASE)
_NON_DEPENDENCY_STATUSES = frozenset({"cancelled", "canceled", "done", "skipped", "superseded"})
_TOKEN_STOPWORDS = frozenset(
    {
        "add",
        "after",
        "and",
        "app",
        "change",
        "create",
        "docs",
        "file",
        "fix",
        "for",
        "from",
        "implementation",
        "make",
        "modify",
        "new",
        "src",
        "task",
        "test",
        "tests",
        "the",
        "then",
        "this",
        "update",
        "with",
    }
)


@dataclass(frozen=True)
class InferredTaskDependency:
    task_id: str
    depends_on: str
    reason: str


def infer_ordered_predecessor_dependency(
    *,
    tasks: list[dict[str, Any]],
    task: dict[str, Any],
) -> InferredTaskDependency | None:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return None
    if _task_is_read_only(task):
        return None
    deps = _task_dependencies(task)
    if deps:
        return None
    text = _task_text(task)
    if not text or not _ORDERING_SIGNAL_RE.search(text):
        return None

    previous_tasks: list[dict[str, Any]] = []
    for candidate in tasks:
        if candidate is task or str(candidate.get("id") or "").strip() == task_id:
            break
        previous_tasks.append(candidate)

    current_tokens = _dependency_tokens(text)
    explicit_previous = bool(_EXPLICIT_PREVIOUS_RE.search(text))
    for previous in reversed(previous_tasks):
        previous_id = str(previous.get("id") or "").strip()
        if not previous_id or previous_id in deps:
            continue
        if _canonical_status(str(previous.get("status") or "")) in _NON_DEPENDENCY_STATUSES:
            continue
        if _task_is_read_only(previous) and not explicit_previous:
            continue
        previous_tokens = _dependency_tokens(_task_text(previous))
        if explicit_previous or current_tokens & previous_tokens:
            return InferredTaskDependency(
                task_id=task_id,
                depends_on=previous_id,
                reason="ordered predecessor cue",
            )
    return None


def apply_ordered_dependency_inference(
    *,
    tasks: list[dict[str, Any]],
    touched_task_ids: set[str],
) -> tuple[bool, list[InferredTaskDependency]]:
    changed = False
    inferred: list[InferredTaskDependency] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "").strip()
        if task_id not in touched_task_ids:
            continue
        if _canonical_status(str(task.get("status") or "")) != "planned":
            continue
        dependency = infer_ordered_predecessor_dependency(tasks=tasks, task=task)
        if dependency is None:
            continue
        deps = _task_dependencies(task)
        if dependency.depends_on in deps:
            continue
        task["dependencies"] = [*deps, dependency.depends_on]
        inferred.append(dependency)
        changed = True
    return changed, inferred


def _task_dependencies(task: dict[str, Any]) -> list[str]:
    deps = task.get("dependencies") or []
    if not isinstance(deps, list):
        return []
    return [str(dep).strip() for dep in deps if str(dep).strip()]


def _task_is_read_only(task: dict[str, Any]) -> bool:
    return is_clearly_non_mutating_task(
        title=str(task.get("title") or "").strip(),
        description=str(task.get("description") or "").strip(),
        acceptance_criteria=normalized_text_list(task.get("acceptance_criteria")),
    )


def _task_text(task: dict[str, Any]) -> str:
    parts = [
        str(task.get("title") or ""),
        str(task.get("description") or ""),
        *normalized_text_list(task.get("acceptance_criteria")),
        *normalized_text_list(task.get("estimated_files")),
        *normalized_text_list(task.get("write_scope")),
    ]
    return "\n".join(parts)


def _dependency_tokens(text: str) -> set[str]:
    normalized = re.sub(r"[./\\_-]+", " ", str(text or "").casefold())
    tokens: set[str] = set()
    for raw in _WORD_RE.findall(normalized):
        token = raw.casefold()
        if len(token) <= 2 or token in _TOKEN_STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def _canonical_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value == "todo":
        return "planned"
    return value or "planned"
