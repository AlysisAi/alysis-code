from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .atomic_io import atomic_write_text
from .forge import RunPaths

KNOWLEDGE_INDEX_SCHEMA_VERSION = 5
KnowledgeEntryKind = Literal["task_attempt", "issue", "fact", "decision"]
_SUPPORTED_KNOWLEDGE_KINDS: tuple[KnowledgeEntryKind, ...] = (
    "task_attempt",
    "issue",
    "fact",
    "decision",
)
OPEN_ISSUE_STATUSES = frozenset({"open", "active", "blocked"})
RESOLVED_ISSUE_STATUSES = frozenset({"resolved", "closed"})
ACCEPTED_TASK_ATTEMPT_STATUSES = frozenset({"accepted"})
PENDING_TASK_ATTEMPT_STATUSES = frozenset({"pending"})
REJECTED_TASK_ATTEMPT_STATUSES = frozenset({"rejected"})
_LIFECYCLE_DERIVED_ENTRY_KINDS = frozenset({"issue", "decision", "task_attempt"})
_KNOWLEDGE_INDEX_REBUILD_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _normalize_path_value(value: str) -> str:
    cleaned = str(value).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def _sanitize_component(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in str(value).strip())
    return safe or "item"


def _timestamp_slug(value: str) -> str:
    return (
        value.replace("-", "")
        .replace(":", "")
        .replace("+00:00", "Z")
        .replace("T", "T")
        .replace(".", "")
    )


def _repo_rel(root: Path, path: Path) -> str:
    return os.fspath(path.resolve().relative_to(root.resolve()))


def _trim_preview(text: str, *, max_chars: int = 160) -> str:
    raw = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(raw) <= max_chars:
        return raw
    if max_chars <= 3:
        return raw[:max_chars]
    return raw[: max_chars - 3].rstrip() + "..."


def _body_preview(body: str) -> str:
    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return _trim_preview(" ".join(lines))


def _frontmatter_scalar(value: Any) -> str:
    if value is None:
        return "null"
    return json.dumps(str(value), ensure_ascii=False)


def _parse_frontmatter_scalar(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _serialize_frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_frontmatter_scalar(item)}")
            continue
        lines.append(f"{key}: {_frontmatter_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("knowledge entry is missing YAML frontmatter")
    frontmatter_lines: list[str] = []
    body_start = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            body_start = idx + 1
            break
        frontmatter_lines.append(lines[idx])
    if body_start is None:
        raise ValueError("knowledge entry frontmatter is not terminated")

    data: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in frontmatter_lines:
        stripped = line.rstrip()
        if stripped.startswith("  - "):
            if current_list_key is None:
                raise ValueError("knowledge entry list item missing key")
            items = data.setdefault(current_list_key, [])
            if not isinstance(items, list):
                raise ValueError("knowledge entry frontmatter list state is invalid")
            items.append(str(_parse_frontmatter_scalar(stripped[4:])))
            continue
        current_list_key = None
        if ":" not in stripped:
            raise ValueError(f"invalid knowledge frontmatter line: {stripped}")
        key, raw_value = stripped.split(":", 1)
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("knowledge entry frontmatter key cannot be empty")
        if raw_value.strip():
            data[normalized_key] = _parse_frontmatter_scalar(raw_value)
            continue
        data[normalized_key] = []
        current_list_key = normalized_key

    body = "\n".join(lines[body_start:]).rstrip() + "\n"
    return data, body


@dataclass(frozen=True)
class KnowledgeEntry:
    kind: KnowledgeEntryKind
    id: str
    title: str
    created_at: str
    task_id: str
    source: str
    status: str
    result: str | None = None
    paths: tuple[str, ...] = ()
    related_tasks: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    report_path: str | None = None
    patch_path: str | None = None
    verify_artifact_path: str | None = None
    budget_artifact_path: str | None = None
    session_artifact_dir: str | None = None
    signature: str | None = None
    decision_key: str | None = None
    resolves: tuple[str, ...] = ()
    capture_artifact_path: str | None = None
    body: str = ""
    file_path: Path | None = None

    def to_frontmatter_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "task_id": self.task_id,
            "source": self.source,
            "status": self.status,
            "result": self.result,
            "paths": list(self.paths),
            "related_tasks": list(self.related_tasks),
            "tags": list(self.tags),
            "report_path": self.report_path,
            "patch_path": self.patch_path,
            "verify_artifact_path": self.verify_artifact_path,
            "budget_artifact_path": self.budget_artifact_path,
            "session_artifact_dir": self.session_artifact_dir,
            "signature": self.signature,
            "decision_key": self.decision_key,
            "resolves": list(self.resolves),
            "capture_artifact_path": self.capture_artifact_path,
        }

    def to_markdown(self) -> str:
        return (
            _serialize_frontmatter(self.to_frontmatter_payload()) + "\n" + self.body.rstrip() + "\n"
        )

    @classmethod
    def from_markdown(cls, *, text: str, file_path: Path | None = None) -> KnowledgeEntry:
        frontmatter, body = _parse_frontmatter(text)
        kind = str(frontmatter.get("kind") or "").strip()
        if kind not in _SUPPORTED_KNOWLEDGE_KINDS:
            raise ValueError(f"unsupported knowledge kind: {kind}")
        return cls(
            kind=kind,  # type: ignore[arg-type]
            id=str(frontmatter.get("id") or "").strip(),
            title=str(frontmatter.get("title") or "").strip(),
            created_at=str(frontmatter.get("created_at") or "").strip(),
            task_id=str(frontmatter.get("task_id") or "").strip(),
            source=str(frontmatter.get("source") or "").strip(),
            status=_normalize_task_attempt_status(
                kind=kind,
                status=str(frontmatter.get("status") or "").strip(),
                result=_optional_text(frontmatter.get("result")),
            ),
            result=_normalize_task_attempt_result(
                kind=kind,
                result=_optional_text(frontmatter.get("result")),
                status=str(frontmatter.get("status") or "").strip(),
            ),
            paths=tuple(
                str(item).strip() for item in frontmatter.get("paths") or [] if str(item).strip()
            ),
            related_tasks=tuple(
                str(item).strip()
                for item in frontmatter.get("related_tasks") or []
                if str(item).strip()
            ),
            tags=tuple(
                str(item).strip() for item in frontmatter.get("tags") or [] if str(item).strip()
            ),
            report_path=_optional_text(frontmatter.get("report_path")),
            patch_path=_optional_text(frontmatter.get("patch_path")),
            verify_artifact_path=_optional_text(frontmatter.get("verify_artifact_path")),
            budget_artifact_path=_optional_text(frontmatter.get("budget_artifact_path")),
            session_artifact_dir=_optional_text(frontmatter.get("session_artifact_dir")),
            signature=_optional_text(frontmatter.get("signature")),
            decision_key=_optional_text(frontmatter.get("decision_key")),
            resolves=tuple(
                str(item).strip() for item in frontmatter.get("resolves") or [] if str(item).strip()
            ),
            capture_artifact_path=_optional_text(frontmatter.get("capture_artifact_path")),
            body=body,
            file_path=file_path,
        )


@dataclass(frozen=True)
class KnowledgeIndexEntry:
    id: str
    kind: KnowledgeEntryKind
    title: str
    preview: str
    run_id: str
    task_id: str
    source: str
    status: str
    tags: tuple[str, ...]
    paths: tuple[str, ...]
    related_tasks: tuple[str, ...]
    created_at: str
    knowledge_file_path: str
    result: str | None = None
    signature: str | None = None
    decision_key: str | None = None
    resolves: tuple[str, ...] = ()
    capture_artifact_path: str | None = None
    effective_status: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "preview": self.preview,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "source": self.source,
            "status": self.status,
            "result": self.result,
            "tags": list(self.tags),
            "paths": list(self.paths),
            "related_tasks": list(self.related_tasks),
            "created_at": self.created_at,
            "knowledge_file_path": self.knowledge_file_path,
            "signature": self.signature,
            "decision_key": self.decision_key,
            "resolves": list(self.resolves),
            "capture_artifact_path": self.capture_artifact_path,
            "effective_status": self.effective_status or self.status,
        }


@dataclass(frozen=True)
class KnowledgeIndex:
    path: Path
    entries: tuple[KnowledgeIndexEntry, ...]
    generated_at: str
    invalid_entries: tuple[KnowledgeIndexInvalidEntry, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": KNOWLEDGE_INDEX_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "entry_count": len(self.entries),
            "invalid_entry_count": len(self.invalid_entries),
            "invalid_entries": [entry.to_payload() for entry in self.invalid_entries],
            "entries": [entry.to_payload() for entry in self.entries],
        }


@dataclass(frozen=True)
class KnowledgeIndexInvalidEntry:
    knowledge_file_path: str
    error: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "knowledge_file_path": self.knowledge_file_path,
            "error": self.error,
        }


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_optional_signature(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_resolve_ids(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item).strip() for item in (values or []) if str(item).strip()))


def _knowledge_index_payload_is_compatible(raw: dict[str, Any]) -> bool:
    if raw.get("schema_version") != KNOWLEDGE_INDEX_SCHEMA_VERSION:
        return False
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return False
    for item in entries:
        if not isinstance(item, dict):
            return False
        kind = str(item.get("kind") or "").strip()
        if (
            kind in _LIFECYCLE_DERIVED_ENTRY_KINDS
            and not str(item.get("effective_status") or "").strip()
        ):
            return False
    return True


def _normalize_task_attempt_result(*, kind: str, result: str | None, status: str) -> str | None:
    if kind != "task_attempt":
        return result
    text = str(result or "").strip()
    if text:
        return text
    legacy_status = str(status or "").strip()
    if legacy_status in {"success", "failure"}:
        return legacy_status
    return None


def _normalize_task_attempt_status(*, kind: str, status: str, result: str | None) -> str:
    if kind != "task_attempt":
        return status
    text = str(status or "").strip()
    if text in {"accepted", "pending", "rejected"}:
        return text
    if text == "success":
        return "accepted"
    if text == "failure":
        return "rejected"
    normalized_result = str(result or "").strip()
    if normalized_result == "success":
        return "accepted"
    if normalized_result == "failure":
        return "rejected"
    return text


def derive_run_id_from_knowledge_file_path(knowledge_file_path: str | Path | None) -> str:
    if knowledge_file_path is None:
        return ""
    parts = Path(str(knowledge_file_path)).parts
    for index in range(len(parts) - 2):
        if parts[index] == ".alysis" and parts[index + 1] == "runs":
            return parts[index + 2]
    return ""


def is_effectively_open_status(status: str | None) -> bool:
    return str(status or "").strip() in OPEN_ISSUE_STATUSES


def is_effectively_accepted_task_attempt(status: str | None) -> bool:
    return str(status or "").strip() in ACCEPTED_TASK_ATTEMPT_STATUSES


def _derive_effective_issue_statuses(records: list[KnowledgeEntry]) -> dict[str, str]:
    issues = [record for record in records if record.kind == "issue"]
    effective_statuses = {record.id: record.status for record in issues}
    issue_ids = set(effective_statuses)
    for record in issues:
        if record.status not in RESOLVED_ISSUE_STATUSES or not record.resolves:
            continue
        for resolved_id in record.resolves:
            if resolved_id in issue_ids and is_effectively_open_status(
                effective_statuses.get(resolved_id)
            ):
                effective_statuses[resolved_id] = "resolved"
    return effective_statuses


def _derive_effective_decision_statuses(records: list[KnowledgeEntry]) -> dict[str, str]:
    effective_statuses = {
        record.id: record.status for record in records if record.kind == "decision"
    }
    decisions_by_key: dict[str, list[KnowledgeEntry]] = {}
    for record in records:
        if record.kind != "decision" or not record.decision_key:
            continue
        decisions_by_key.setdefault(record.decision_key, []).append(record)
    for decision_records in decisions_by_key.values():
        latest = max(
            decision_records,
            key=lambda record: (
                record.created_at,
                record.id,
                record.file_path.as_posix() if record.file_path is not None else "",
            ),
        )
        for record in decision_records:
            effective_statuses[record.id] = latest.status
    return effective_statuses


def _derive_effective_task_attempt_statuses(records: list[KnowledgeEntry]) -> dict[str, str]:
    attempts = [record for record in records if record.kind == "task_attempt"]
    effective_statuses = {record.id: record.status for record in attempts}
    ordered_attempts = sorted(
        attempts,
        key=lambda record: (
            record.created_at,
            record.id,
            record.file_path.as_posix() if record.file_path is not None else "",
        ),
    )
    attempt_ids = set(effective_statuses)
    for record in ordered_attempts:
        if record.kind != "task_attempt" or not record.resolves:
            continue
        for resolved_id in record.resolves:
            if resolved_id in attempt_ids:
                effective_statuses[resolved_id] = record.status
    return effective_statuses


def _entry_kind_dir(paths: RunPaths, kind: KnowledgeEntryKind) -> Path:
    if kind == "task_attempt":
        return paths.knowledge_task_attempts_dir
    if kind == "issue":
        return paths.knowledge_issues_dir
    if kind == "fact":
        return paths.knowledge_facts_dir
    return paths.knowledge_decisions_dir


def _knowledge_bucket_dir(paths: RunPaths, *, kind: KnowledgeEntryKind, task_id: str) -> Path:
    bucket = _entry_kind_dir(paths, kind)
    task_component = _sanitize_component(task_id or "general")
    directory = bucket / task_component
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_text_atomic(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _unique_entry_identity(
    *, kind: KnowledgeEntryKind, task_id: str, source: str, created_at: str
) -> tuple[str, str]:
    suffix = uuid4().hex[:8]
    identity = f"{kind}-{_sanitize_component(task_id)}-{_sanitize_component(source)}-{_timestamp_slug(created_at)}-{suffix}"
    filename_stem = f"{_timestamp_slug(created_at)}_{_sanitize_component(source)}_{suffix}"
    return identity, filename_stem


def _publish_markdown_entry_atomic(*, directory: Path, stem: str, markdown_text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    final_path = directory / f"{stem}.md"
    if final_path.exists():
        raise FileExistsError(f"knowledge entry already exists: {final_path}")
    fd, temp_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{stem}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(markdown_text)
        os.replace(temp_path, final_path)
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()
    return final_path


def list_workspace_knowledge_entry_paths(root: Path) -> list[Path]:
    knowledge_root = root.resolve() / ".alysis" / "runs"
    if not knowledge_root.exists():
        return []
    paths: list[Path] = []
    for kind_dir_name in ("task_attempts", "issues", "facts", "decisions"):
        for path in sorted(knowledge_root.glob(f"*/knowledge/{kind_dir_name}/**/*.md")):
            if path.is_file():
                paths.append(path)
    return paths


def load_knowledge_entry(path: Path) -> KnowledgeEntry:
    return KnowledgeEntry.from_markdown(
        text=path.read_text(encoding="utf-8"),
        file_path=path,
    )


def _invalid_entry_error(exc: Exception) -> str:
    return _trim_preview(f"{type(exc).__name__}: {exc}", max_chars=240)


def rebuild_knowledge_index(paths: RunPaths) -> KnowledgeIndex:
    """Rebuild the shared workspace index without concurrent replace races."""
    with _KNOWLEDGE_INDEX_REBUILD_LOCK:
        return _rebuild_knowledge_index_unlocked(paths)


def _rebuild_knowledge_index_unlocked(paths: RunPaths) -> KnowledgeIndex:
    records: list[KnowledgeEntry] = []
    invalid_entries: list[KnowledgeIndexInvalidEntry] = []
    for path in list_workspace_knowledge_entry_paths(paths.root):
        try:
            record = load_knowledge_entry(path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            invalid_entries.append(
                KnowledgeIndexInvalidEntry(
                    knowledge_file_path=_repo_rel(paths.root, path),
                    error=_invalid_entry_error(exc),
                )
            )
            continue
        records.append(record)
    effective_issue_statuses = _derive_effective_issue_statuses(records)
    effective_decision_statuses = _derive_effective_decision_statuses(records)
    effective_task_attempt_statuses = _derive_effective_task_attempt_statuses(records)
    entries: list[KnowledgeIndexEntry] = []
    for record in records:
        if record.file_path is None:
            continue
        entries.append(
            KnowledgeIndexEntry(
                id=record.id,
                kind=record.kind,
                title=record.title,
                preview=_body_preview(record.body),
                run_id=derive_run_id_from_knowledge_file_path(
                    _repo_rel(paths.root, record.file_path)
                ),
                task_id=record.task_id,
                source=record.source,
                status=record.status,
                result=record.result,
                tags=tuple(record.tags),
                paths=tuple(_normalize_path_value(item) for item in record.paths if item.strip()),
                related_tasks=tuple(record.related_tasks),
                created_at=record.created_at,
                knowledge_file_path=_repo_rel(paths.root, record.file_path),
                signature=record.signature,
                decision_key=record.decision_key,
                resolves=record.resolves,
                capture_artifact_path=record.capture_artifact_path,
                effective_status=(
                    effective_task_attempt_statuses.get(record.id)
                    or effective_issue_statuses.get(record.id)
                    or effective_decision_statuses.get(record.id)
                    or record.status
                ),
            )
        )

    entries.sort(
        key=lambda entry: (
            entry.created_at,
            entry.kind,
            entry.id,
            entry.knowledge_file_path,
        ),
        reverse=True,
    )
    invalid_entries.sort(
        key=lambda entry: (
            entry.knowledge_file_path,
            entry.error,
        )
    )
    index = KnowledgeIndex(
        path=paths.knowledge_index_path,
        entries=tuple(entries),
        generated_at=_now_iso(),
        invalid_entries=tuple(invalid_entries),
    )
    _write_text_atomic(
        paths.knowledge_index_path,
        json.dumps(index.to_payload(), indent=2, sort_keys=True) + "\n",
    )
    return index


def load_knowledge_index(paths: RunPaths, *, rebuild: bool = False) -> KnowledgeIndex:
    """Load the shared index without racing a Windows atomic replacement."""
    with _KNOWLEDGE_INDEX_REBUILD_LOCK:
        return _load_knowledge_index_unlocked(paths, rebuild=rebuild)


def _load_knowledge_index_unlocked(paths: RunPaths, *, rebuild: bool = False) -> KnowledgeIndex:
    if rebuild or not paths.knowledge_index_path.exists():
        return rebuild_knowledge_index(paths)
    try:
        raw = json.loads(paths.knowledge_index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return rebuild_knowledge_index(paths)
    if not isinstance(raw, dict):
        return rebuild_knowledge_index(paths)
    if not _knowledge_index_payload_is_compatible(raw):
        return rebuild_knowledge_index(paths)
    try:
        entries = tuple(
            KnowledgeIndexEntry(
                id=str(item.get("id") or "").strip(),
                kind=str(item.get("kind") or "").strip(),  # type: ignore[arg-type]
                title=str(item.get("title") or "").strip(),
                preview=str(item.get("preview") or "").strip(),
                run_id=(
                    str(item.get("run_id") or "").strip()
                    or derive_run_id_from_knowledge_file_path(item.get("knowledge_file_path"))
                ),
                task_id=str(item.get("task_id") or "").strip(),
                source=str(item.get("source") or "").strip(),
                status=_normalize_task_attempt_status(
                    kind=str(item.get("kind") or "").strip(),
                    status=str(item.get("status") or "").strip(),
                    result=_optional_text(item.get("result")),
                ),
                result=_normalize_task_attempt_result(
                    kind=str(item.get("kind") or "").strip(),
                    result=_optional_text(item.get("result")),
                    status=str(item.get("status") or "").strip(),
                ),
                tags=tuple(str(tag).strip() for tag in item.get("tags") or [] if str(tag).strip()),
                paths=tuple(
                    str(path).strip() for path in item.get("paths") or [] if str(path).strip()
                ),
                related_tasks=tuple(
                    str(task_id).strip()
                    for task_id in item.get("related_tasks") or []
                    if str(task_id).strip()
                ),
                created_at=str(item.get("created_at") or "").strip(),
                knowledge_file_path=str(item.get("knowledge_file_path") or "").strip(),
                signature=_normalize_optional_signature(item.get("signature")),
                decision_key=_optional_text(item.get("decision_key")),
                resolves=_normalize_resolve_ids(item.get("resolves")),
                capture_artifact_path=_optional_text(item.get("capture_artifact_path")),
                effective_status=_normalize_task_attempt_status(
                    kind=str(item.get("kind") or "").strip(),
                    status=str(item.get("effective_status") or item.get("status") or "").strip(),
                    result=_optional_text(item.get("result")),
                ),
            )
            for item in raw.get("entries") or []
            if isinstance(item, dict)
        )
        invalid_entries = tuple(
            KnowledgeIndexInvalidEntry(
                knowledge_file_path=str(item.get("knowledge_file_path") or "").strip(),
                error=str(item.get("error") or "").strip(),
            )
            for item in raw.get("invalid_entries") or []
            if isinstance(item, dict)
        )
    except AttributeError:
        return rebuild_knowledge_index(paths)
    return KnowledgeIndex(
        path=paths.knowledge_index_path,
        entries=entries,
        generated_at=str(raw.get("generated_at") or "").strip(),
        invalid_entries=invalid_entries,
    )


def load_task_attempt_entries_for_task(
    *, paths: RunPaths, task_id: str
) -> tuple[KnowledgeEntry, ...]:
    directory = paths.knowledge_task_attempts_dir / str(task_id).strip()
    if not directory.exists():
        return ()
    entries: list[KnowledgeEntry] = []
    for path in sorted(directory.glob("*.md")):
        if not path.is_file():
            continue
        try:
            entry = load_knowledge_entry(path)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if entry.kind == "task_attempt":
            entries.append(entry)
    return tuple(entries)


def find_latest_task_attempt_entry(
    *,
    paths: RunPaths,
    task_id: str,
    source: str | None = None,
    include_resolution_entries: bool = False,
) -> KnowledgeEntry | None:
    entries = load_task_attempt_entries_for_task(paths=paths, task_id=task_id)
    candidates = [
        entry
        for entry in entries
        if (source is None or entry.source == source)
        and (include_resolution_entries or not entry.resolves)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda entry: (
            entry.created_at,
            entry.id,
            entry.file_path.as_posix() if entry.file_path is not None else "",
        ),
    )


def _has_task_attempt_resolution(
    *,
    paths: RunPaths,
    task_id: str,
    resolved_attempt_id: str,
    acceptance_state: str,
) -> bool:
    return any(
        resolved_attempt_id in entry.resolves and entry.status == acceptance_state
        for entry in load_task_attempt_entries_for_task(paths=paths, task_id=task_id)
    )


def write_task_attempt_entry(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    source: str,
    result: str | None,
    summary: str,
    changed_files: list[str],
    verify_summary: str | None,
    report_path: Path | None,
    patch_path: Path | None,
    verify_artifact_path: Path | None,
    budget_artifact_path: Path | None,
    session_artifact_dir: Path | None,
    acceptance_state: str | None = None,
    resolves: list[str] | tuple[str, ...] | None = None,
    created_at: str | None = None,
    extra_tags: list[str] | tuple[str, ...] = (),
) -> KnowledgeEntry:
    created = created_at or _now_iso()
    task_id = str(task.get("id") or "").strip() or "task"
    task_title = str(task.get("title") or "").strip() or task_id
    entry_id, filename_stem = _unique_entry_identity(
        kind="task_attempt",
        task_id=task_id,
        source=source,
        created_at=created,
    )
    related_tasks = tuple(
        item for item in (str(dep).strip() for dep in task.get("dependencies") or []) if item
    )
    raw_result = str(result or "").strip() or None
    normalized_acceptance_state = str(acceptance_state or "").strip() or (
        "accepted" if raw_result == "success" else "rejected"
    )
    resolve_ids = _normalize_resolve_ids(resolves)
    fallback_paths = [
        *[str(path).strip() for path in task.get("write_scope") or []],
        *[str(path).strip() for path in task.get("estimated_files") or []],
    ]
    effective_paths = tuple(
        dict.fromkeys(
            _normalize_path_value(path)
            for path in (list(changed_files) or fallback_paths)
            if _normalize_path_value(str(path))
        )
    )
    body_lines = [
        f"# Task Attempt: {task_id}",
        "",
        f"- Raw Result: `{raw_result}`" if raw_result else "- Raw Result: (resolution only)",
        f"- Acceptance State: `{normalized_acceptance_state}`",
        f"- Source: `{source}`",
        f"- Summary: {summary or '(none)'}",
        "",
        "## Changed Files",
        "",
    ]
    if effective_paths:
        body_lines.extend(f"- `{path}`" for path in effective_paths)
    else:
        body_lines.append("- (none)")
    body_lines.extend(
        [
            "",
            "## Lifecycle",
            "",
        ]
    )
    if resolve_ids:
        body_lines.extend(f"- Resolves: `{item}`" for item in resolve_ids)
    else:
        body_lines.append("- Resolves: (none)")
    body_lines.extend(
        [
            "",
            "## Verification",
            "",
            f"- Summary: {verify_summary or '(none)'}",
            "",
            "## Related Artifacts",
            "",
            f"- Report: `{_repo_rel(paths.root, report_path)}`"
            if report_path
            else "- Report: (none)",
            f"- Patch: `{_repo_rel(paths.root, patch_path)}`" if patch_path else "- Patch: (none)",
            (
                f"- Verify Artifact: `{_repo_rel(paths.root, verify_artifact_path)}`"
                if verify_artifact_path
                else "- Verify Artifact: (none)"
            ),
            (
                f"- Budget Artifact: `{_repo_rel(paths.root, budget_artifact_path)}`"
                if budget_artifact_path
                else "- Budget Artifact: (none)"
            ),
            (
                f"- Session Artifacts: `{_repo_rel(paths.root, session_artifact_dir)}`"
                if session_artifact_dir
                else "- Session Artifacts: (none retained)"
            ),
        ]
    )
    entry = KnowledgeEntry(
        kind="task_attempt",
        id=entry_id,
        title=f"{task_id}: {task_title}",
        created_at=created,
        task_id=task_id,
        source=source,
        status=normalized_acceptance_state,
        result=raw_result,
        paths=effective_paths,
        related_tasks=related_tasks,
        tags=tuple(
            dict.fromkeys(
                [
                    source,
                    normalized_acceptance_state,
                    *([raw_result] if raw_result else []),
                    *[str(tag).strip() for tag in extra_tags if str(tag).strip()],
                ]
            )
        ),
        report_path=_repo_rel(paths.root, report_path) if report_path else None,
        patch_path=_repo_rel(paths.root, patch_path) if patch_path else None,
        verify_artifact_path=(
            _repo_rel(paths.root, verify_artifact_path) if verify_artifact_path else None
        ),
        budget_artifact_path=(
            _repo_rel(paths.root, budget_artifact_path) if budget_artifact_path else None
        ),
        session_artifact_dir=(
            _repo_rel(paths.root, session_artifact_dir) if session_artifact_dir else None
        ),
        resolves=resolve_ids,
        body="\n".join(body_lines).rstrip() + "\n",
    )
    directory = _knowledge_bucket_dir(paths, kind="task_attempt", task_id=task_id)
    file_path = _publish_markdown_entry_atomic(
        directory=directory,
        stem=filename_stem,
        markdown_text=entry.to_markdown(),
    )
    return KnowledgeEntry.from_markdown(
        text=file_path.read_text(encoding="utf-8"), file_path=file_path
    )


def write_task_attempt_resolution_entry(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    source: str,
    acceptance_state: str,
    resolved_attempt_id: str,
    summary: str,
    changed_files: list[str],
    verify_summary: str | None,
    report_path: Path | None,
    patch_path: Path | None,
    verify_artifact_path: Path | None,
    budget_artifact_path: Path | None,
    session_artifact_dir: Path | None,
    created_at: str | None = None,
    extra_tags: list[str] | tuple[str, ...] = (),
) -> KnowledgeEntry:
    task_id = str(task.get("id") or "").strip() or "task"
    if _has_task_attempt_resolution(
        paths=paths,
        task_id=task_id,
        resolved_attempt_id=resolved_attempt_id,
        acceptance_state=acceptance_state,
    ):
        existing_entries = [
            entry
            for entry in load_task_attempt_entries_for_task(paths=paths, task_id=task_id)
            if entry.source == source
            and entry.status == acceptance_state
            and resolved_attempt_id in entry.resolves
        ]
        if existing_entries:
            return max(
                existing_entries,
                key=lambda entry: (
                    entry.created_at,
                    entry.id,
                    entry.file_path.as_posix() if entry.file_path is not None else "",
                ),
            )
    return write_task_attempt_entry(
        paths=paths,
        task=task,
        source=source,
        result=None,
        summary=summary,
        changed_files=changed_files,
        verify_summary=verify_summary,
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_artifact_path,
        budget_artifact_path=budget_artifact_path,
        session_artifact_dir=session_artifact_dir,
        acceptance_state=acceptance_state,
        resolves=[resolved_attempt_id],
        created_at=created_at,
        extra_tags=[*extra_tags, "acceptance_resolution"],
    )


def write_issue_entry(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    source: str,
    title: str,
    summary: str,
    paths_in_scope: list[str] | tuple[str, ...] = (),
    report_path: Path | None = None,
    patch_path: Path | None = None,
    verify_artifact_path: Path | None = None,
    budget_artifact_path: Path | None = None,
    session_artifact_dir: Path | None = None,
    related_tasks: list[str] | tuple[str, ...] | None = None,
    tags: list[str] | tuple[str, ...] = (),
    created_at: str | None = None,
    status: str = "open",
    signature: str | None = None,
    resolves: list[str] | tuple[str, ...] | None = None,
) -> KnowledgeEntry:
    task_id = str(task.get("id") or "").strip() or "task"
    related = related_tasks or [
        str(dep).strip() for dep in task.get("dependencies") or [] if str(dep).strip()
    ]
    return write_issue_entry_for_task_id(
        paths=paths,
        task_id=task_id,
        source=source,
        title=title,
        summary=summary,
        paths_in_scope=paths_in_scope,
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_artifact_path,
        budget_artifact_path=budget_artifact_path,
        session_artifact_dir=session_artifact_dir,
        related_tasks=related,
        tags=tags,
        created_at=created_at,
        status=status,
        signature=signature,
        resolves=resolves,
    )


def write_issue_entry_for_task_id(
    *,
    paths: RunPaths,
    task_id: str,
    source: str,
    title: str,
    summary: str,
    paths_in_scope: list[str] | tuple[str, ...] = (),
    report_path: Path | None = None,
    patch_path: Path | None = None,
    verify_artifact_path: Path | None = None,
    budget_artifact_path: Path | None = None,
    session_artifact_dir: Path | None = None,
    related_tasks: list[str] | tuple[str, ...] | None = None,
    tags: list[str] | tuple[str, ...] = (),
    created_at: str | None = None,
    status: str = "open",
    signature: str | None = None,
    resolves: list[str] | tuple[str, ...] | None = None,
) -> KnowledgeEntry:
    created = created_at or _now_iso()
    entry_id, filename_stem = _unique_entry_identity(
        kind="issue",
        task_id=task_id,
        source=source,
        created_at=created,
    )
    normalized_paths = tuple(
        dict.fromkeys(
            _normalize_path_value(path)
            for path in paths_in_scope
            if _normalize_path_value(str(path))
        )
    )
    related = tuple(
        dict.fromkeys(str(item).strip() for item in (related_tasks or []) if str(item).strip())
    )
    normalized_signature = _normalize_optional_signature(signature)
    resolve_ids = _normalize_resolve_ids(resolves)
    body_lines = [
        f"# Issue: {title}",
        "",
        f"- Status: `{status}`",
        f"- Source: `{source}`",
        f"- Signature: `{normalized_signature}`" if normalized_signature else "- Signature: (none)",
        f"- Summary: {summary or '(none)'}",
        "",
        "## Related Paths",
        "",
    ]
    if normalized_paths:
        body_lines.extend(f"- `{path}`" for path in normalized_paths)
    else:
        body_lines.append("- (none)")
    body_lines.extend(["", "## Lifecycle", ""])
    if resolve_ids:
        body_lines.extend(f"- Resolves: `{item}`" for item in resolve_ids)
    else:
        body_lines.append("- Resolves: (none)")
    body_lines.extend(
        [
            "",
            "## Related Artifacts",
            "",
            f"- Report: `{_repo_rel(paths.root, report_path)}`"
            if report_path
            else "- Report: (none)",
            f"- Patch: `{_repo_rel(paths.root, patch_path)}`" if patch_path else "- Patch: (none)",
            (
                f"- Verify Artifact: `{_repo_rel(paths.root, verify_artifact_path)}`"
                if verify_artifact_path
                else "- Verify Artifact: (none)"
            ),
            (
                f"- Budget Artifact: `{_repo_rel(paths.root, budget_artifact_path)}`"
                if budget_artifact_path
                else "- Budget Artifact: (none)"
            ),
            (
                f"- Session Artifacts: `{_repo_rel(paths.root, session_artifact_dir)}`"
                if session_artifact_dir
                else "- Session Artifacts: (none retained)"
            ),
        ]
    )
    entry = KnowledgeEntry(
        kind="issue",
        id=entry_id,
        title=title,
        created_at=created,
        task_id=task_id,
        source=source,
        status=status,
        paths=normalized_paths,
        related_tasks=related,
        tags=tuple(
            dict.fromkeys([source, status, *[str(tag).strip() for tag in tags if str(tag).strip()]])
        ),
        report_path=_repo_rel(paths.root, report_path) if report_path else None,
        patch_path=_repo_rel(paths.root, patch_path) if patch_path else None,
        verify_artifact_path=(
            _repo_rel(paths.root, verify_artifact_path) if verify_artifact_path else None
        ),
        budget_artifact_path=(
            _repo_rel(paths.root, budget_artifact_path) if budget_artifact_path else None
        ),
        session_artifact_dir=(
            _repo_rel(paths.root, session_artifact_dir) if session_artifact_dir else None
        ),
        signature=normalized_signature,
        resolves=resolve_ids,
        body="\n".join(body_lines).rstrip() + "\n",
    )
    directory = _knowledge_bucket_dir(paths, kind="issue", task_id=task_id)
    file_path = _publish_markdown_entry_atomic(
        directory=directory,
        stem=filename_stem,
        markdown_text=entry.to_markdown(),
    )
    return KnowledgeEntry.from_markdown(
        text=file_path.read_text(encoding="utf-8"), file_path=file_path
    )


def write_fact_entry(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    source: str,
    title: str,
    summary: str,
    paths_in_scope: list[str] | tuple[str, ...] = (),
    report_path: Path | None = None,
    patch_path: Path | None = None,
    verify_artifact_path: Path | None = None,
    budget_artifact_path: Path | None = None,
    session_artifact_dir: Path | None = None,
    capture_artifact_path: Path | None = None,
    related_tasks: list[str] | tuple[str, ...] | None = None,
    tags: list[str] | tuple[str, ...] = (),
    created_at: str | None = None,
) -> KnowledgeEntry:
    created = created_at or _now_iso()
    task_id = str(task.get("id") or "").strip() or "task"
    entry_id, filename_stem = _unique_entry_identity(
        kind="fact",
        task_id=task_id,
        source=source,
        created_at=created,
    )
    normalized_paths = tuple(
        dict.fromkeys(
            _normalize_path_value(path)
            for path in paths_in_scope
            if _normalize_path_value(str(path))
        )
    )
    related = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (
                related_tasks
                or [str(dep).strip() for dep in task.get("dependencies") or [] if str(dep).strip()]
            )
            if str(item).strip()
        )
    )
    capture_rel = _repo_rel(paths.root, capture_artifact_path) if capture_artifact_path else None
    body_lines = [
        f"# Fact: {title}",
        "",
        "- Status: `recorded`",
        f"- Source: `{source}`",
        f"- Summary: {summary or '(none)'}",
        "",
        "## Related Paths",
        "",
    ]
    if normalized_paths:
        body_lines.extend(f"- `{path}`" for path in normalized_paths)
    else:
        body_lines.append("- (none)")
    body_lines.extend(
        [
            "",
            "## Related Artifacts",
            "",
            f"- Report: `{_repo_rel(paths.root, report_path)}`"
            if report_path
            else "- Report: (none)",
            f"- Patch: `{_repo_rel(paths.root, patch_path)}`" if patch_path else "- Patch: (none)",
            (
                f"- Verify Artifact: `{_repo_rel(paths.root, verify_artifact_path)}`"
                if verify_artifact_path
                else "- Verify Artifact: (none)"
            ),
            (
                f"- Budget Artifact: `{_repo_rel(paths.root, budget_artifact_path)}`"
                if budget_artifact_path
                else "- Budget Artifact: (none)"
            ),
            (
                f"- Session Artifacts: `{_repo_rel(paths.root, session_artifact_dir)}`"
                if session_artifact_dir
                else "- Session Artifacts: (none retained)"
            ),
            f"- Capture Artifact: `{capture_rel}`" if capture_rel else "- Capture Artifact: (none)",
        ]
    )
    entry = KnowledgeEntry(
        kind="fact",
        id=entry_id,
        title=title,
        created_at=created,
        task_id=task_id,
        source=source,
        status="recorded",
        paths=normalized_paths,
        related_tasks=related,
        tags=tuple(
            dict.fromkeys(["fact", source, *[str(tag).strip() for tag in tags if str(tag).strip()]])
        ),
        report_path=_repo_rel(paths.root, report_path) if report_path else None,
        patch_path=_repo_rel(paths.root, patch_path) if patch_path else None,
        verify_artifact_path=(
            _repo_rel(paths.root, verify_artifact_path) if verify_artifact_path else None
        ),
        budget_artifact_path=(
            _repo_rel(paths.root, budget_artifact_path) if budget_artifact_path else None
        ),
        session_artifact_dir=(
            _repo_rel(paths.root, session_artifact_dir) if session_artifact_dir else None
        ),
        capture_artifact_path=capture_rel,
        body="\n".join(body_lines).rstrip() + "\n",
    )
    directory = _knowledge_bucket_dir(paths, kind="fact", task_id=task_id)
    file_path = _publish_markdown_entry_atomic(
        directory=directory,
        stem=filename_stem,
        markdown_text=entry.to_markdown(),
    )
    return KnowledgeEntry.from_markdown(
        text=file_path.read_text(encoding="utf-8"), file_path=file_path
    )


def write_decision_entry(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    source: str,
    decision_key: str,
    title: str,
    summary: str,
    status: str,
    paths_in_scope: list[str] | tuple[str, ...] = (),
    report_path: Path | None = None,
    patch_path: Path | None = None,
    verify_artifact_path: Path | None = None,
    budget_artifact_path: Path | None = None,
    session_artifact_dir: Path | None = None,
    capture_artifact_path: Path | None = None,
    related_tasks: list[str] | tuple[str, ...] | None = None,
    tags: list[str] | tuple[str, ...] = (),
    created_at: str | None = None,
) -> KnowledgeEntry:
    created = created_at or _now_iso()
    task_id = str(task.get("id") or "").strip() or "task"
    entry_id, filename_stem = _unique_entry_identity(
        kind="decision",
        task_id=task_id,
        source=source,
        created_at=created,
    )
    normalized_paths = tuple(
        dict.fromkeys(
            _normalize_path_value(path)
            for path in paths_in_scope
            if _normalize_path_value(str(path))
        )
    )
    related = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (
                related_tasks
                or [str(dep).strip() for dep in task.get("dependencies") or [] if str(dep).strip()]
            )
            if str(item).strip()
        )
    )
    capture_rel = _repo_rel(paths.root, capture_artifact_path) if capture_artifact_path else None
    body_lines = [
        f"# Decision: {title}",
        "",
        f"- Decision Key: `{decision_key}`",
        f"- Status: `{status}`",
        f"- Source: `{source}`",
        f"- Summary: {summary or '(none)'}",
        "",
        "## Related Paths",
        "",
    ]
    if normalized_paths:
        body_lines.extend(f"- `{path}`" for path in normalized_paths)
    else:
        body_lines.append("- (none)")
    body_lines.extend(
        [
            "",
            "## Related Artifacts",
            "",
            f"- Report: `{_repo_rel(paths.root, report_path)}`"
            if report_path
            else "- Report: (none)",
            f"- Patch: `{_repo_rel(paths.root, patch_path)}`" if patch_path else "- Patch: (none)",
            (
                f"- Verify Artifact: `{_repo_rel(paths.root, verify_artifact_path)}`"
                if verify_artifact_path
                else "- Verify Artifact: (none)"
            ),
            (
                f"- Budget Artifact: `{_repo_rel(paths.root, budget_artifact_path)}`"
                if budget_artifact_path
                else "- Budget Artifact: (none)"
            ),
            (
                f"- Session Artifacts: `{_repo_rel(paths.root, session_artifact_dir)}`"
                if session_artifact_dir
                else "- Session Artifacts: (none retained)"
            ),
            f"- Capture Artifact: `{capture_rel}`" if capture_rel else "- Capture Artifact: (none)",
        ]
    )
    entry = KnowledgeEntry(
        kind="decision",
        id=entry_id,
        title=title,
        created_at=created,
        task_id=task_id,
        source=source,
        status=status,
        paths=normalized_paths,
        related_tasks=related,
        tags=tuple(
            dict.fromkeys(
                [
                    "decision",
                    source,
                    status,
                    *[str(tag).strip() for tag in tags if str(tag).strip()],
                ]
            )
        ),
        report_path=_repo_rel(paths.root, report_path) if report_path else None,
        patch_path=_repo_rel(paths.root, patch_path) if patch_path else None,
        verify_artifact_path=(
            _repo_rel(paths.root, verify_artifact_path) if verify_artifact_path else None
        ),
        budget_artifact_path=(
            _repo_rel(paths.root, budget_artifact_path) if budget_artifact_path else None
        ),
        session_artifact_dir=(
            _repo_rel(paths.root, session_artifact_dir) if session_artifact_dir else None
        ),
        decision_key=decision_key,
        capture_artifact_path=capture_rel,
        body="\n".join(body_lines).rstrip() + "\n",
    )
    directory = _knowledge_bucket_dir(paths, kind="decision", task_id=task_id)
    file_path = _publish_markdown_entry_atomic(
        directory=directory,
        stem=filename_stem,
        markdown_text=entry.to_markdown(),
    )
    return KnowledgeEntry.from_markdown(
        text=file_path.read_text(encoding="utf-8"), file_path=file_path
    )
