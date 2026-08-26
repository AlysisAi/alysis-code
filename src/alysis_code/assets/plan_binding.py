from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .models import AssetError


@dataclass(frozen=True)
class AssetBriefingEntry:
    asset_id: str
    rationale: str
    expected_use: str


@dataclass(frozen=True)
class TaskAssetBriefing:
    primary: list[AssetBriefingEntry]
    may_need: list[AssetBriefingEntry]


_ASSET_INTENT_RE = re.compile(
    r"\b(?:asset|attach(?:ed|ment)?|brief|design|image|mockup|note|pdf|"
    r"screenshot|spec|uploaded|wireframe)\b",
    re.IGNORECASE,
)
_SINGLE_ASSET_REFERENCE_RE = re.compile(
    r"\b(?:asset|attach(?:ed|ment)?|brief|image|mockup|pdf|screenshot|uploaded|wireframe)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*", re.IGNORECASE)
_TOKEN_STOPWORDS = frozenset(
    {
        "and",
        "for",
        "from",
        "into",
        "the",
        "this",
        "that",
        "task",
        "use",
        "using",
        "with",
    }
)
_IMPLICIT_ASSET_LIMIT = 3


def parse_task_asset_briefing(payload: Any | None) -> TaskAssetBriefing | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise AssetError("asset_briefing must be an object")
    _reject_unknown_keys(payload, allowed={"primary", "may_need"}, field="asset_briefing")
    return TaskAssetBriefing(
        primary=_parse_entries(payload.get("primary"), field="asset_briefing.primary"),
        may_need=_parse_entries(payload.get("may_need"), field="asset_briefing.may_need"),
    )


def serialize_task_asset_briefing(briefing: TaskAssetBriefing | None) -> dict[str, Any] | None:
    if briefing is None:
        return None
    return {
        "primary": [_entry_to_dict(entry) for entry in briefing.primary],
        "may_need": [_entry_to_dict(entry) for entry in briefing.may_need],
    }


def collect_referenced_asset_ids(plan: dict[str, Any]) -> set[str]:
    referenced: set[str] = set()
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        briefing = task_asset_briefing(task)
        if briefing is None:
            continue
        referenced.update(entry.asset_id for entry in briefing.primary)
        referenced.update(entry.asset_id for entry in briefing.may_need)
    return referenced


def task_asset_briefing(task: dict[str, Any]) -> TaskAssetBriefing | None:
    return parse_task_asset_briefing(task.get("asset_briefing"))


def task_asset_briefing_for_execution(
    task: dict[str, Any],
    *,
    records: Iterable[Any],
) -> TaskAssetBriefing | None:
    briefing = task_asset_briefing(task)
    if briefing is not None and (briefing.primary or briefing.may_need):
        return briefing
    return infer_implicit_task_asset_briefing(task=task, records=records)


def infer_implicit_task_asset_briefing(
    *,
    task: dict[str, Any],
    records: Iterable[Any],
) -> TaskAssetBriefing | None:
    active_records = [
        record
        for record in records
        if getattr(record, "deleted_at", None) is None
        and not bool(getattr(record, "pinned", False))
    ]
    if not active_records:
        return None
    task_text = _task_text(task)
    if not task_text:
        return None
    has_asset_intent = bool(_ASSET_INTENT_RE.search(task_text))
    has_single_asset_reference = bool(_SINGLE_ASSET_REFERENCE_RE.search(task_text))
    scored: list[tuple[int, int, Any]] = []
    for index, record in enumerate(active_records):
        score = _asset_match_score(
            task_text=task_text,
            record=record,
            single_active_asset=len(active_records) == 1,
            has_asset_intent=has_asset_intent,
            has_single_asset_reference=has_single_asset_reference,
        )
        if _asset_score_is_relevant(score=score, has_asset_intent=has_asset_intent):
            scored.append((score, index, record))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    entries = [
        AssetBriefingEntry(
            asset_id=str(getattr(record, "id", "") or "").strip(),
            rationale=_implicit_asset_rationale(record),
            expected_use="Read this asset before executing; it supplies task requirements or constraints.",
        )
        for _score, _index, record in scored[:_IMPLICIT_ASSET_LIMIT]
        if str(getattr(record, "id", "") or "").strip()
    ]
    if not entries:
        return None
    return TaskAssetBriefing(primary=entries, may_need=[])


def bind_asset_to_matching_tasks(
    *,
    plan: dict[str, Any],
    record: Any,
    active_records: Iterable[Any],
) -> list[str]:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return []
    active = [
        item
        for item in active_records
        if getattr(item, "deleted_at", None) is None and not bool(getattr(item, "pinned", False))
    ]
    bound_task_ids: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if _task_references_asset(task, str(getattr(record, "id", "") or "")):
            continue
        try:
            current = task_asset_briefing(task)
        except AssetError:
            continue
        if current is not None and (current.primary or current.may_need):
            continue
        inferred = infer_implicit_task_asset_briefing(task=task, records=active)
        if inferred is None or not any(
            entry.asset_id == str(getattr(record, "id", "") or "").strip()
            for entry in inferred.primary
        ):
            continue
        task["asset_briefing"] = serialize_task_asset_briefing(inferred)
        task_id = str(task.get("id") or "").strip()
        if task_id:
            bound_task_ids.append(task_id)
    return bound_task_ids


def _parse_entries(payload: Any, *, field: str) -> list[AssetBriefingEntry]:
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise AssetError(f"{field} must be an array")
    out: list[AssetBriefingEntry] = []
    for index, raw_entry in enumerate(payload):
        entry_field = f"{field}[{index}]"
        if not isinstance(raw_entry, dict):
            raise AssetError(f"{entry_field} must be an object")
        _reject_unknown_keys(
            raw_entry,
            allowed={"asset_id", "rationale", "expected_use"},
            field=entry_field,
        )
        asset_id = _required_string(raw_entry.get("asset_id"), field=f"{entry_field}.asset_id")
        rationale = _required_string(raw_entry.get("rationale"), field=f"{entry_field}.rationale")
        expected_use = _required_string(
            raw_entry.get("expected_use"),
            field=f"{entry_field}.expected_use",
        )
        out.append(
            AssetBriefingEntry(
                asset_id=asset_id,
                rationale=rationale,
                expected_use=expected_use,
            )
        )
    return out


def _entry_to_dict(entry: AssetBriefingEntry) -> dict[str, str]:
    return {
        "asset_id": entry.asset_id,
        "rationale": entry.rationale,
        "expected_use": entry.expected_use,
    }


def _required_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise AssetError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise AssetError(f"{field} must be non-empty")
    return text


def _reject_unknown_keys(payload: dict[str, Any], *, allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AssetError(f"{field} contains unsupported keys: {', '.join(unknown)}")


def _task_text(task: dict[str, Any]) -> str:
    parts = [
        str(task.get("id") or ""),
        str(task.get("title") or ""),
        str(task.get("description") or ""),
    ]
    acceptance = task.get("acceptance_criteria")
    if isinstance(acceptance, list):
        parts.extend(str(item or "") for item in acceptance)
    scopes = [task.get("estimated_files"), task.get("write_scope")]
    for scope in scopes:
        if isinstance(scope, list):
            parts.extend(str(item or "") for item in scope)
    return "\n".join(parts)


def _asset_match_score(
    *,
    task_text: str,
    record: Any,
    single_active_asset: bool,
    has_asset_intent: bool,
    has_single_asset_reference: bool,
) -> int:
    folded_task = task_text.casefold()
    asset_id = str(getattr(record, "id", "") or "").strip().casefold()
    title = str(getattr(record, "title", "") or "").strip()
    filename = str(getattr(record, "original_filename", "") or "").strip()
    description = str(getattr(record, "description", "") or "").strip()
    stored_path = str(getattr(record, "stored_path", "") or "").strip()
    fields = [title, filename, description, stored_path]
    score = 0
    if asset_id and asset_id in folded_task:
        score += 20
    for field in fields:
        folded = field.casefold()
        if folded and folded in folded_task:
            score += 8
    for token in _asset_tokens(title):
        if token in folded_task:
            score += 4
    for token in _asset_tokens(filename):
        if token in folded_task:
            score += 4
    for token in _asset_tokens(description):
        if token in folded_task:
            score += 2
    if has_single_asset_reference and single_active_asset:
        score += 8
    if has_asset_intent and score >= 4:
        score += 2
    return score


def _asset_score_is_relevant(*, score: int, has_asset_intent: bool) -> bool:
    if score >= 8:
        return True
    return has_asset_intent and score > 0


def _asset_tokens(text: str) -> set[str]:
    normalized = re.sub(r"[./\\_-]+", " ", str(text or "").casefold())
    tokens = set()
    for token in _WORD_RE.findall(normalized):
        if len(token) <= 2 or token in _TOKEN_STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def _implicit_asset_rationale(record: Any) -> str:
    title = str(getattr(record, "title", "") or "").strip()
    if title:
        return f'Task text references an attached or named asset matching "{title}".'
    return "Task text references an attached or named asset matching this record."


def _task_references_asset(task: dict[str, Any], asset_id: str) -> bool:
    if not asset_id:
        return False
    try:
        briefing = task_asset_briefing(task)
    except AssetError:
        return False
    if briefing is None:
        return False
    return any(entry.asset_id == asset_id for entry in [*briefing.primary, *briefing.may_need])
