from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from .forge import RunPaths
from .knowledge_base import (
    KnowledgeIndexEntry,
    derive_run_id_from_knowledge_file_path,
    is_effectively_accepted_task_attempt,
    is_effectively_open_status,
    load_knowledge_index,
    rebuild_knowledge_index,
)
from .task_scope import extract_repo_path_hints

KnowledgeConsumer = Literal["execution", "planner", "replanner"]

_STOPWORDS = {
    "a",
    "an",
    "and",
    "before",
    "for",
    "from",
    "how",
    "if",
    "in",
    "into",
    "is",
    "issue",
    "issues",
    "it",
    "of",
    "on",
    "or",
    "plan",
    "planner",
    "planning",
    "replanner",
    "request",
    "requests",
    "task",
    "tasks",
    "the",
    "this",
    "to",
    "we",
    "work",
    "works",
    "why",
}


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sanitize_component(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in str(value).strip())
    return safe or "item"


def _normalize_rel_path(value: str) -> str:
    cleaned = str(value).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def _is_run_local_task_id_token(token: str) -> bool:
    return bool(re.fullmatch(r"t\d+", token))


def _tokenize(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9_./-]+", text.lower())
        if len(token) >= 3 and token not in _STOPWORDS and not _is_run_local_task_id_token(token)
    }
    return tokens


def _task_paths(task: dict[str, Any], *, extra_paths: list[str] | None = None) -> set[str]:
    values = [
        *[str(path).strip() for path in task.get("write_scope") or []],
        *[str(path).strip() for path in task.get("estimated_files") or []],
        *[str(path).strip() for path in extra_paths or []],
    ]
    return {_normalize_rel_path(value) for value in values if _normalize_rel_path(value)}


def _task_keywords(task: dict[str, Any]) -> set[str]:
    parts = [
        str(task.get("title") or "").strip(),
        str(task.get("description") or "").strip(),
        *[str(item).strip() for item in task.get("acceptance_criteria") or []],
    ]
    return _tokenize(" ".join(parts))


def _task_id_hints(text: str) -> list[str]:
    seen: list[str] = []
    for raw in re.findall(r"\bT\d+\b", text.upper()):
        if raw not in seen:
            seen.append(raw)
    return seen


def _path_value(candidate: Any) -> Path | None:
    if candidate is None:
        return None
    try:
        return Path(candidate).expanduser().resolve()
    except TypeError:
        return None


def _infer_workspace_root_from_artifact(candidate: Path | None) -> Path | None:
    if candidate is None:
        return None
    probe = candidate if candidate.is_dir() else candidate.parent
    for path in (probe, *probe.parents):
        if path.name == ".alysis":
            return path.parent.resolve()
    return None


def resolve_knowledge_workspace_root(paths: Any) -> Path:
    root = _path_value(getattr(paths, "root", None))
    if root is not None:
        return root
    for attr in (
        "plan_dir",
        "plan_json_path",
        "plan_md_path",
        "planner_chat_path",
        "planner_summary_path",
        "notes_path",
    ):
        inferred = _infer_workspace_root_from_artifact(_path_value(getattr(paths, attr, None)))
        if inferred is not None:
            return inferred
    return Path(".").resolve()


def _planner_plan_dir(paths: Any) -> Path:
    plan_dir = _path_value(getattr(paths, "plan_dir", None))
    if plan_dir is not None:
        return plan_dir
    plan_json_path = _path_value(getattr(paths, "plan_json_path", None))
    if plan_json_path is not None:
        return plan_json_path.parent
    planner_chat_path = _path_value(getattr(paths, "planner_chat_path", None))
    if planner_chat_path is not None:
        return planner_chat_path.parent.parent
    planner_summary_path = _path_value(getattr(paths, "planner_summary_path", None))
    if planner_summary_path is not None:
        return planner_summary_path.parent.parent
    root = resolve_knowledge_workspace_root(paths)
    return root / ".alysis" / "planner"


def _coerce_planner_paths(paths: Any) -> Any:
    root = resolve_knowledge_workspace_root(paths)
    plan_dir = _planner_plan_dir(paths)
    run_id = str(getattr(paths, "run_id", "") or "").strip()
    if not run_id:
        run_id = plan_dir.parent.name
    knowledge_index_path = _path_value(getattr(paths, "knowledge_index_path", None))
    if knowledge_index_path is None:
        knowledge_index_path = plan_dir.parent / "knowledge" / "index.json"
    knowledge_selected_dir = _path_value(getattr(paths, "knowledge_selected_dir", None))
    if knowledge_selected_dir is None:
        knowledge_selected_dir = plan_dir.parent / "knowledge" / "selected"
    return SimpleNamespace(
        root=root,
        run_id=run_id,
        plan_dir=plan_dir,
        knowledge_index_path=knowledge_index_path,
        knowledge_selected_dir=knowledge_selected_dir,
    )


def _entry_keywords(entry: KnowledgeIndexEntry) -> set[str]:
    return _tokenize(
        " ".join(
            [
                entry.title,
                entry.preview,
                *entry.tags,
                *entry.paths,
            ]
        )
    )


def _parse_iso(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


@dataclass(frozen=True)
class KnowledgeSelection:
    entry: KnowledgeIndexEntry
    score: int
    reasons: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        payload = self.entry.to_payload()
        payload["score"] = self.score
        payload["reasons"] = list(self.reasons)
        return payload


@dataclass(frozen=True)
class MaterializedKnowledgeSelection:
    task_id: str
    selection_label: str
    selected_dir: Path
    entries_dir: Path
    manifest_path: Path
    summary_path: Path
    selections: tuple[KnowledgeSelection, ...]

    def render_prompt_section(self, *, workspace_root: Path) -> str:
        manifest_rel = self.manifest_path.resolve().relative_to(workspace_root.resolve()).as_posix()
        entries_rel = self.entries_dir.resolve().relative_to(workspace_root.resolve()).as_posix()
        lines = [
            "## Relevant Knowledge",
            "",
            f"- Manifest: `{manifest_rel}`",
            f"- Selected Knowledge Files: `{entries_rel}`",
        ]
        if not self.selections:
            lines.append("- No prior knowledge entries were selected for this task.")
            return "\n".join(lines)

        for selection in self.selections:
            reason_text = (
                "; ".join(selection.reasons[:3]) if selection.reasons else "baseline relevance"
            )
            state_suffix = ""
            effective_status = selection.entry.effective_status or selection.entry.status
            if selection.entry.kind in {"issue", "decision"}:
                state_suffix = f"; state={effective_status}"
            lines.append(
                f"- `{selection.entry.kind}` `{selection.entry.id}`: {selection.entry.title} "
                f"(score={selection.score}{state_suffix}; {reason_text}). Preview: {selection.entry.preview or '(none)'}"
            )
        return "\n".join(lines)


def _current_issue_status(entry: KnowledgeIndexEntry) -> str:
    return entry.effective_status or entry.status


def _current_decision_status(entry: KnowledgeIndexEntry) -> str:
    return entry.effective_status or entry.status


def _consumer_issue_boost(*, consumer: KnowledgeConsumer) -> int:
    if consumer == "replanner":
        return 7
    if consumer == "planner":
        return 5
    return 4


def _consumer_decision_boost(*, consumer: KnowledgeConsumer) -> int:
    if consumer == "replanner":
        return 5
    if consumer == "planner":
        return 4
    return 3


def _consumer_fact_boost(*, consumer: KnowledgeConsumer) -> int:
    return 2 if consumer in {"planner", "replanner"} else 1


def _consumer_task_attempt_boost(*, consumer: KnowledgeConsumer) -> int:
    if consumer == "replanner":
        return 3
    if consumer == "planner":
        return 1
    return 0


def _entry_sort_priority(entry: KnowledgeIndexEntry, *, consumer: KnowledgeConsumer) -> int:
    if consumer == "replanner":
        order = {"issue": 4, "decision": 3, "task_attempt": 2, "fact": 1}
    elif consumer == "planner":
        order = {"decision": 4, "issue": 3, "fact": 2, "task_attempt": 1}
    else:
        order = {"issue": 4, "decision": 3, "fact": 2, "task_attempt": 1}
    return order.get(entry.kind, 0)


def _resolve_current_run_id(paths: Any) -> str:
    run_id = str(getattr(paths, "run_id", "") or "").strip()
    if run_id:
        return run_id
    for attr in ("knowledge_index_path", "plan_dir", "plan_json_path", "planner_chat_path"):
        path_value = _path_value(getattr(paths, attr, None))
        if path_value is None:
            continue
        derived = derive_run_id_from_knowledge_file_path(path_value)
        if derived:
            return derived
    return ""


def _score_entry(
    *,
    task: dict[str, Any],
    entry: KnowledgeIndexEntry,
    extra_paths: list[str] | None,
    now: datetime,
    consumer: KnowledgeConsumer,
    current_run_id: str,
) -> KnowledgeSelection | None:
    if entry.kind == "task_attempt" and entry.resolves:
        return None
    current_task_id = str(task.get("id") or "").strip()
    task_paths = _task_paths(task, extra_paths=extra_paths)
    task_keywords = _task_keywords(task)
    dependency_ids = {
        str(item).strip() for item in task.get("dependencies") or [] if str(item).strip()
    }

    score = 0
    reasons: list[str] = []
    same_run = bool(current_run_id and entry.run_id and current_run_id == entry.run_id)

    normalized_entry_paths = {
        _normalize_rel_path(path) for path in entry.paths if _normalize_rel_path(path)
    }
    path_overlap = sorted(task_paths.intersection(normalized_entry_paths))
    if path_overlap:
        score += 8 + min(4, len(path_overlap))
        reasons.append(f"path overlap: {', '.join(path_overlap[:3])}")

    conflict_overlap = sorted(
        {
            _normalize_rel_path(path) for path in extra_paths or [] if _normalize_rel_path(path)
        }.intersection(normalized_entry_paths)
    )
    if conflict_overlap:
        score += 5 + min(3, len(conflict_overlap))
        reasons.append(f"conflict overlap: {', '.join(conflict_overlap[:3])}")

    if same_run and entry.task_id and entry.task_id == current_task_id:
        score += 6
        reasons.append("same task history")

    dependency_overlap = (
        sorted(dependency_ids.intersection({entry.task_id, *entry.related_tasks}))
        if same_run
        else []
    )
    if dependency_overlap:
        score += 5 + min(2, len(dependency_overlap))
        reasons.append(f"dependency link: {', '.join(dependency_overlap[:3])}")

    keyword_overlap = sorted(task_keywords.intersection(_entry_keywords(entry)))
    if keyword_overlap:
        score += min(8, len(keyword_overlap) * 2)
        reasons.append(f"keyword overlap: {', '.join(keyword_overlap[:3])}")

    if score <= 0:
        return None

    effective_status = _current_issue_status(entry)
    if entry.kind == "issue" and is_effectively_open_status(effective_status):
        score += _consumer_issue_boost(consumer=consumer)
        reasons.append("open issue boost")
    if entry.kind == "decision" and _current_decision_status(entry) == "active":
        score += _consumer_decision_boost(consumer=consumer)
        reasons.append("active decision boost")
    if entry.kind == "fact" and score > 0:
        score += _consumer_fact_boost(consumer=consumer)
        reasons.append("recorded fact context")
    if (
        entry.kind == "task_attempt"
        and entry.result == "success"
        and is_effectively_accepted_task_attempt(entry.effective_status or entry.status)
        and score > 0
    ):
        bonus = _consumer_task_attempt_boost(consumer=consumer)
        if bonus:
            score += bonus
            reasons.append("accepted task attempt context")

    created_at = _parse_iso(entry.created_at)
    if created_at is not None:
        age_seconds = max(0.0, (now - created_at).total_seconds())
        if age_seconds <= 7 * 24 * 3600:
            score += 3
            reasons.append("recent context")
        elif age_seconds <= 30 * 24 * 3600:
            score += 1
            reasons.append("moderately recent context")

    return KnowledgeSelection(
        entry=entry,
        score=score,
        reasons=tuple(reasons),
    )


def select_relevant_knowledge(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    extra_paths: list[str] | None = None,
    limit: int = 4,
    consumer: KnowledgeConsumer = "execution",
) -> tuple[KnowledgeSelection, ...]:
    index = load_knowledge_index(paths, rebuild=False)
    now = datetime.now(UTC)
    current_run_id = _resolve_current_run_id(paths)
    scored: list[KnowledgeSelection] = []
    for entry in index.entries:
        selection = _score_entry(
            task=task,
            entry=entry,
            extra_paths=extra_paths,
            now=now,
            consumer=consumer,
            current_run_id=current_run_id,
        )
        if selection is not None:
            scored.append(selection)
    scored.sort(
        key=lambda item: (
            item.score,
            _entry_sort_priority(item.entry, consumer=consumer),
            item.entry.created_at,
            item.entry.id,
        ),
        reverse=True,
    )
    return tuple(scored[: max(0, int(limit))])


def materialize_selected_knowledge(
    *,
    paths: RunPaths,
    task_id: str,
    selection_label: str,
    selections: tuple[KnowledgeSelection, ...],
    selected_dir_override: Path | None = None,
) -> MaterializedKnowledgeSelection:
    selected_dir = (
        selected_dir_override
        if selected_dir_override is not None
        else (
            paths.knowledge_selected_dir
            / _sanitize_component(task_id)
            / _sanitize_component(selection_label)
        )
    )
    entries_dir = selected_dir / "entries"
    if selected_dir.exists():
        shutil.rmtree(selected_dir, ignore_errors=True)
    entries_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries: list[dict[str, Any]] = []
    for index, selection in enumerate(selections, start=1):
        source_path = paths.root / selection.entry.knowledge_file_path
        materialized_path = entries_dir / f"{index:02d}_{source_path.name}"
        shutil.copy2(source_path, materialized_path)
        payload = selection.to_payload()
        payload["materialized_path"] = (
            materialized_path.resolve().relative_to(paths.root.resolve()).as_posix()
        )
        manifest_entries.append(payload)

    manifest_path = selected_dir / "manifest.json"
    manifest_payload = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "task_id": task_id,
        "selection_label": selection_label,
        "manifest_path": manifest_path.resolve().relative_to(paths.root.resolve()).as_posix(),
        "selected_entries": manifest_entries,
    }
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_path = selected_dir / "summary.md"
    section = MaterializedKnowledgeSelection(
        task_id=task_id,
        selection_label=selection_label,
        selected_dir=selected_dir,
        entries_dir=entries_dir,
        manifest_path=manifest_path,
        summary_path=summary_path,
        selections=selections,
    ).render_prompt_section(workspace_root=paths.root)
    summary_path.write_text(section.rstrip() + "\n", encoding="utf-8")
    return MaterializedKnowledgeSelection(
        task_id=task_id,
        selection_label=selection_label,
        selected_dir=selected_dir,
        entries_dir=entries_dir,
        manifest_path=manifest_path,
        summary_path=summary_path,
        selections=selections,
    )


def prepare_relevant_knowledge(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    selection_label: str,
    extra_paths: list[str] | None = None,
    limit: int = 4,
    consumer: KnowledgeConsumer = "execution",
    selected_dir_override: Path | None = None,
) -> MaterializedKnowledgeSelection:
    rebuild_knowledge_index(paths)
    selections = select_relevant_knowledge(
        paths=paths,
        task=task,
        extra_paths=extra_paths,
        limit=limit,
        consumer=consumer,
    )
    task_id = str(task.get("id") or "").strip() or "task"
    return materialize_selected_knowledge(
        paths=paths,
        task_id=task_id,
        selection_label=selection_label,
        selections=selections,
        selected_dir_override=selected_dir_override,
    )


def prepare_planner_knowledge(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    user_text: str,
    selection_label: str = "planner",
    limit: int = 5,
    selected_dir_override: Path | None = None,
) -> MaterializedKnowledgeSelection:
    effective_paths = _coerce_planner_paths(paths)
    task_titles = [
        str(task.get("title") or "").strip()
        for task in plan.get("tasks") or []
        if isinstance(task, dict) and str(task.get("title") or "").strip()
    ]
    title = str(user_text or "").strip() or str(plan.get("project_goal") or "").strip() or "planner"
    description_parts = [
        str(plan.get("project_goal") or "").strip(),
        str(plan.get("summary") or "").strip(),
        "Current plan tasks: " + "; ".join(task_titles[:8]) if task_titles else "",
    ]
    task_id_candidates = {
        str(task.get("id") or "").strip()
        for task in plan.get("tasks") or []
        if isinstance(task, dict) and str(task.get("id") or "").strip()
    }
    hinted_task_ids = [
        task_id for task_id in _task_id_hints(user_text) if task_id in task_id_candidates
    ]
    path_hints = extract_repo_path_hints(user_text)
    synthetic_task = {
        "id": "planner",
        "title": title,
        "description": "\n".join(part for part in description_parts if part),
        "acceptance_criteria": [],
        "estimated_files": path_hints,
        "write_scope": path_hints,
        "dependencies": hinted_task_ids,
    }
    selected_dir = selected_dir_override or (
        effective_paths.plan_dir / "selected_knowledge" / _sanitize_component(selection_label)
    )
    return prepare_relevant_knowledge(
        paths=effective_paths,
        task=synthetic_task,
        selection_label=selection_label,
        extra_paths=path_hints,
        limit=limit,
        consumer="planner",
        selected_dir_override=selected_dir,
    )
