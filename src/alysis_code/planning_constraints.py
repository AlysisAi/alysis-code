from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Literal

from .file_classification import is_code_implementation_path, is_test_path
from .task_scope import (
    extract_forbidden_repo_path_hint_records,
    normalize_repo_path_entry,
    split_normalized_repo_path_list,
)

PLANNING_CONSTRAINTS_KEY = "planning_constraints"
PLANNING_CONSTRAINTS_SCHEMA_VERSION = 1

PlanningConstraintKind = Literal[
    "target_root",
    "forbidden_root",
    "decoy_root",
    "unrelated_root",
]

_PATHLIKE_RE = re.compile(
    r"(?<![\w/-])("
    r"(?:[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
    r"|(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9_*?\[\]-]+)"
    r"|(?:README(?:\.md)?)"
    r"|(?:Dockerfile|Makefile|LICENSE|NOTICE|CHANGELOG)"
    r")(?![\w/-])"
)
_CONTEXT_SPLIT_RE = re.compile(r"[\n;]+|(?<=[.!?])\s+")
_MONOREPO_CONTAINER_NAMES = frozenset(
    {
        "app",
        "apps",
        "component",
        "components",
        "crate",
        "crates",
        "lib",
        "libs",
        "module",
        "modules",
        "package",
        "packages",
        "service",
        "services",
        "workspace",
        "workspaces",
    }
)
_TARGET_DIRECT_RE = re.compile(
    r"\b(?:only|only\s+(?:edit|fix|modify|touch|change|update)|"
    r"stay\s+(?:scoped\s+)?(?:to|inside|within)|scope(?:d)?\s+(?:to|inside|within)|"
    r"target(?:ing)?|target\s+root|switch\s+to|focus\s+on|work\s+on|"
    r"inside|under|within|in|for)\b",
    re.IGNORECASE,
)
_TARGET_INTENT_RE = re.compile(
    r"\b(?:add|adding|build|building|change|changing|configure|configuring|edit|editing|"
    r"enable|enabling|fix|fixing|implement|implementing|improve|improving|modify|"
    r"modifying|patch|patching|repair|repairing|support|supporting|update|updating|"
    r"wire|wiring|work\s+on|working\s+on)\b",
    re.IGNORECASE,
)
_FORBIDDEN_DIRECT_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|must\s+not|never)\s+"
    r"(?:edit|modify|change|touch|write(?:\s+to)?|update|delete|remove|create)\b"
    r"|\b(?:leave|keep)\s+(?:the\s+)?(?:file\s+)?(?:[\w./-]+\s+)?"
    r"(?:unchanged|untouched|unmodified)\b"
    r"|\b(?:remain|stays?|stay|left)\s+(?:unchanged|untouched|unmodified)\b",
    re.IGNORECASE,
)
_FORBIDDEN_AFTER_RE = re.compile(
    r"^\s*(?:file\s+)?(?:is|are|must\s+remain|should\s+remain|stays?|stay|left)?\s*"
    r"(?:not\s+)?(?:untouched|unchanged|unmodified|preserved|not\s+modified|"
    r"not\s+changed|not\s+touched)\b"
    r"|^\s*(?:file\s+)?not\s+(?:modified|changed|touched|written)\b",
    re.IGNORECASE,
)
_DECOY_RE = re.compile(
    r"\b(?:decoy|negative\s+example|example\s+only|not\s+an?\s+(?:target|task))\b",
    re.IGNORECASE,
)
_UNRELATED_RE = re.compile(
    r"\b(?:unrelated|out\s+of\s+scope|not\s+relevant|irrelevant)\b",
    re.IGNORECASE,
)
_IGNORE_RE = re.compile(r"\b(?:ignore|skip|exclude)\b", re.IGNORECASE)
_ONLY_RE = re.compile(r"\bonly\b", re.IGNORECASE)
_ADDITIVE_TARGET_RE = re.compile(
    r"\b(?:also|additionally|as\s+well|too|include)\b",
    re.IGNORECASE,
)
_EXCLUSIVE_TARGET_RE = re.compile(
    r"\b(?:only|stay\s+(?:scoped\s+)?(?:to|inside|within)|"
    r"scope(?:d)?\s+(?:to|inside|within)|target\s+root)\b",
    re.IGNORECASE,
)
_RETARGET_RE = re.compile(
    r"\b(?:actually|instead|now|switch\s+to|retarget(?:\s+to)?|"
    r"change\s+(?:the\s+)?(?:target|scope|focus)\s+to|"
    r"move\s+(?:the\s+)?(?:target|scope|focus)\s+to)\b",
    re.IGNORECASE,
)
_MARKER_WINDOW_CHARS = 80
_SHARED_EVIDENCE_RE = re.compile(
    r"\b(?:shared|common|cross[- ]?package|cross[- ]?service|dependency|depends\s+on|"
    r"library|lib|used\s+by|imported\s+by)\b",
    re.IGNORECASE,
)
_TEST_EVIDENCE_RE = re.compile(r"\b(?:test|tests|spec|coverage|regression)\b", re.IGNORECASE)
_NON_TARGET_SURFACE_RE = re.compile(
    r"^(?:README(?:\.md)?|docs?/|notes?/|changelog(?:\.md)?|LICENSE|NOTICE)",
    re.IGNORECASE,
)
_GLOB_CHARS = ("*", "?", "[")


@dataclass(frozen=True)
class PlanningPathConstraint:
    path: str
    kind: PlanningConstraintKind
    reason_code: str
    evidence: str

    def to_payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "kind": self.kind,
            "reason_code": self.reason_code,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class PlanningScopeConstraints:
    target_roots: tuple[PlanningPathConstraint, ...] = ()
    forbidden_roots: tuple[PlanningPathConstraint, ...] = ()
    decoy_roots: tuple[PlanningPathConstraint, ...] = ()
    unrelated_roots: tuple[PlanningPathConstraint, ...] = ()

    @property
    def has_constraints(self) -> bool:
        return bool(
            self.target_roots or self.forbidden_roots or self.decoy_roots or self.unrelated_roots
        )

    @property
    def blocked_roots(self) -> tuple[PlanningPathConstraint, ...]:
        return (*self.forbidden_roots, *self.decoy_roots, *self.unrelated_roots)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PLANNING_CONSTRAINTS_SCHEMA_VERSION,
            "target_roots": [item.to_payload() for item in self.target_roots],
            "forbidden_roots": [item.to_payload() for item in self.forbidden_roots],
            "decoy_roots": [item.to_payload() for item in self.decoy_roots],
            "unrelated_roots": [item.to_payload() for item in self.unrelated_roots],
        }


@dataclass(frozen=True)
class PlanningScopeViolation:
    path: str
    classification: str
    reason_code: str
    evidence: str
    constraint_path: str | None = None

    def to_payload(self) -> dict[str, str]:
        payload = {
            "path": self.path,
            "classification": self.classification,
            "reason_code": self.reason_code,
            "evidence": self.evidence,
        }
        if self.constraint_path:
            payload["constraint_path"] = self.constraint_path
        return payload


def _dedupe_constraints(
    constraints: list[PlanningPathConstraint],
) -> tuple[PlanningPathConstraint, ...]:
    seen: set[tuple[str, str]] = set()
    out: list[PlanningPathConstraint] = []
    for item in constraints:
        key = (item.kind, _constraint_identity(item.path))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return tuple(out)


def _constraint_identity(path: str) -> str:
    return _constraint_root(path).casefold()


def _constraint_root(path: str) -> str:
    cleaned = str(path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.rstrip("/")
    if cleaned.endswith("/**"):
        cleaned = cleaned[:-3].rstrip("/")
    if "/**/" in cleaned:
        cleaned = cleaned.split("/**/", 1)[0].rstrip("/")
    return cleaned


def _normalize_constraint_path(value: str) -> str | None:
    normalized = normalize_repo_path_entry(str(value or ""), allow_extensionless_file=True)
    if not normalized:
        return None
    return _constraint_root(normalized)


def _looks_like_file_path(path: str) -> bool:
    name = PurePosixPath(_constraint_root(path)).name
    return "." in name or name in {
        "README",
        "Dockerfile",
        "Makefile",
        "LICENSE",
        "NOTICE",
        "CHANGELOG",
    }


def _evidence_fragment(context: str) -> str:
    cleaned = " ".join(str(context or "").strip().split())
    if len(cleaned) <= 180:
        return cleaned
    return cleaned[:177].rstrip() + "..."


def _path_mentions(text: str) -> list[tuple[str, str]]:
    mentions: list[tuple[str, str]] = []
    seen: set[str] = set()
    contexts = [item.strip() for item in _CONTEXT_SPLIT_RE.split(text or "") if item.strip()]
    for context in contexts:
        for match in _PATHLIKE_RE.finditer(context):
            normalized = _normalize_constraint_path(match.group(1))
            if not normalized:
                continue
            key = (context.casefold(), normalized.casefold())
            if str(key) in seen:
                continue
            seen.add(str(key))
            mentions.append((normalized, context))
    return mentions


def _path_context_windows(context: str, path: str) -> tuple[str, str, str]:
    context_text = str(context or "")
    context_cf = context_text.casefold()
    path_cf = str(path or "").casefold()
    start = context_cf.find(path_cf)
    if start < 0:
        return context_text, "", context_text
    end = start + len(path_cf)
    before = context_text[max(0, start - _MARKER_WINDOW_CHARS) : start]
    after = context_text[end : end + _MARKER_WINDOW_CHARS]
    return before + context_text[start:end] + after, before, after


def _negative_marker_applies_before_path(
    *,
    before: str,
    marker_re: re.Pattern[str],
) -> bool:
    matches = list(marker_re.finditer(before))
    if not matches:
        return False
    last = matches[-1]
    between = before[last.end() :]
    return _TARGET_INTENT_RE.search(between) is None


def _context_reason_for_path(context: str, path: str) -> tuple[PlanningConstraintKind, str] | None:
    local, before, after = _path_context_windows(context, path)
    if _FORBIDDEN_AFTER_RE.search(after) or _negative_marker_applies_before_path(
        before=before,
        marker_re=_FORBIDDEN_DIRECT_RE,
    ):
        return "forbidden_root", "direct_path_forbidden_instruction"
    if _DECOY_RE.search(after) or _negative_marker_applies_before_path(
        before=before,
        marker_re=_DECOY_RE,
    ):
        return "decoy_root", "decoy_path_constraint"
    if _UNRELATED_RE.search(after) or _negative_marker_applies_before_path(
        before=before,
        marker_re=_UNRELATED_RE,
    ):
        return "unrelated_root", "unrelated_path_constraint"
    if _IGNORE_RE.search(after) or _negative_marker_applies_before_path(
        before=before,
        marker_re=_IGNORE_RE,
    ):
        return "decoy_root", "ignored_path_constraint"
    if not _looks_like_file_path(path) and (
        _TARGET_DIRECT_RE.search(local) or _TARGET_INTENT_RE.search(local)
    ):
        return "target_root", "user_target_root"
    return None


def _append_constraint(
    items: list[PlanningPathConstraint],
    *,
    path: str,
    kind: PlanningConstraintKind,
    reason_code: str,
    evidence: str,
) -> None:
    normalized = _normalize_constraint_path(path)
    if normalized is None:
        return
    items.append(
        PlanningPathConstraint(
            path=normalized,
            kind=kind,
            reason_code=reason_code,
            evidence=_evidence_fragment(evidence),
        )
    )


def _workspace_candidate_roots(workspace_context: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(workspace_context, dict):
        return ()
    roots: list[str] = []
    for entry in workspace_context.get("manifests") or []:
        if not isinstance(entry, dict):
            continue
        path = _normalize_constraint_path(str(entry.get("path") or ""))
        if not path or "/" not in path:
            continue
        parent = path.rsplit("/", 1)[0]
        if parent and parent != ".":
            roots.append(parent)
    for path in workspace_context.get("observed_paths") or []:
        normalized = _normalize_constraint_path(str(path or ""))
        if not normalized or "/" not in normalized:
            continue
        parts = [part for part in normalized.split("/") if part]
        if len(parts) >= 2 and parts[0].casefold() in _MONOREPO_CONTAINER_NAMES:
            roots.append("/".join(parts[:2]))
    focus_relpath = _normalize_constraint_path(str(workspace_context.get("focus_relpath") or ""))
    if focus_relpath and focus_relpath != "." and "/" in focus_relpath:
        roots.append(focus_relpath)

    seen: set[str] = set()
    out: list[str] = []
    for root in roots:
        key = root.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return tuple(out)


def _workspace_known_scope_paths(workspace_context: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(workspace_context, dict):
        return ()
    raw_paths: list[str] = []
    raw_paths.extend(str(item or "") for item in workspace_context.get("observed_paths") or [])
    raw_paths.extend(str(item or "") for item in workspace_context.get("readme_paths") or [])
    conventions_path = str(workspace_context.get("conventions_path") or "").strip()
    if conventions_path:
        raw_paths.append(conventions_path)
    for entry in workspace_context.get("manifests") or []:
        if isinstance(entry, dict):
            raw_paths.append(str(entry.get("path") or ""))
    for entry in workspace_context.get("top_level_entries") or []:
        if isinstance(entry, dict):
            raw_paths.append(str(entry.get("path") or ""))

    known: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        normalized = _normalize_constraint_path(raw)
        if not normalized:
            continue
        parts = [part for part in normalized.split("/") if part]
        candidates = [normalized]
        if len(parts) > 1:
            candidates.extend("/".join(parts[:index]) for index in range(1, len(parts)))
        for candidate in candidates:
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            known.append(candidate)
    return tuple(known)


def _path_is_grounded_in_workspace_context(
    path: str,
    *,
    workspace_context: dict[str, Any] | None,
) -> bool:
    if not isinstance(workspace_context, dict):
        return True
    normalized = _normalize_constraint_path(path)
    if not normalized:
        return False
    path_key = _constraint_root(normalized).casefold()
    if not path_key:
        return False
    for candidate in (
        *_workspace_candidate_roots(workspace_context),
        *_workspace_known_scope_paths(workspace_context),
    ):
        candidate_key = _constraint_root(candidate).casefold()
        if not candidate_key:
            continue
        if (
            path_key == candidate_key
            or candidate_key.startswith(path_key + "/")
            or ("/" in candidate_key and path_key.startswith(candidate_key + "/"))
        ):
            return True
    return False


def _candidate_roots_by_leaf(workspace_context: dict[str, Any] | None) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for root in _workspace_candidate_roots(workspace_context):
        leaf = root.rsplit("/", 1)[-1].casefold()
        if not leaf:
            continue
        grouped.setdefault(leaf, []).append(root)
    return {
        leaf: roots[0]
        for leaf, roots in grouped.items()
        if len({root.casefold() for root in roots}) == 1
    }


def _word_present(text: str, word: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text, re.I) is not None


def _word_context_windows(context: str, word: str) -> tuple[str, str, str] | None:
    match = re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", context, re.I)
    if match is None:
        return None
    start, end = match.span()
    before = context[max(0, start - _MARKER_WINDOW_CHARS) : start]
    after = context[end : end + _MARKER_WINDOW_CHARS]
    return before + context[start:end] + after, before, after


def _leaf_name_constraints(
    text: str,
    *,
    workspace_context: dict[str, Any] | None,
) -> list[PlanningPathConstraint]:
    by_leaf = _candidate_roots_by_leaf(workspace_context)
    if not by_leaf:
        return []
    constraints: list[PlanningPathConstraint] = []
    contexts = [item.strip() for item in _CONTEXT_SPLIT_RE.split(text or "") if item.strip()]
    for context in contexts:
        context_cf = context.casefold()
        for leaf, root in by_leaf.items():
            if not _word_present(context_cf, leaf):
                continue
            windows = _word_context_windows(context, leaf)
            if windows is None:
                continue
            local, before, after = windows
            if _FORBIDDEN_AFTER_RE.search(after) or _negative_marker_applies_before_path(
                before=before,
                marker_re=_FORBIDDEN_DIRECT_RE,
            ):
                _append_constraint(
                    constraints,
                    path=root,
                    kind="forbidden_root",
                    reason_code="direct_path_forbidden_instruction",
                    evidence=context,
                )
            elif _DECOY_RE.search(after) or _negative_marker_applies_before_path(
                before=before,
                marker_re=_DECOY_RE,
            ):
                _append_constraint(
                    constraints,
                    path=root,
                    kind="decoy_root",
                    reason_code="decoy_path_constraint",
                    evidence=context,
                )
            elif (
                _UNRELATED_RE.search(after)
                or _negative_marker_applies_before_path(before=before, marker_re=_UNRELATED_RE)
                or _IGNORE_RE.search(after)
                or _negative_marker_applies_before_path(before=before, marker_re=_IGNORE_RE)
            ):
                _append_constraint(
                    constraints,
                    path=root,
                    kind="unrelated_root",
                    reason_code="unrelated_path_constraint",
                    evidence=context,
                )
            elif (
                _ONLY_RE.search(local)
                or _TARGET_DIRECT_RE.search(local)
                or _TARGET_INTENT_RE.search(local)
            ):
                _append_constraint(
                    constraints,
                    path=root,
                    kind="target_root",
                    reason_code="user_target_root_name",
                    evidence=context,
                )
    return constraints


def extract_planning_scope_constraints(
    text: str,
    *,
    workspace_context: dict[str, Any] | None = None,
) -> PlanningScopeConstraints:
    raw_constraints: list[PlanningPathConstraint] = []
    for forbidden in extract_forbidden_repo_path_hint_records(text or ""):
        _append_constraint(
            raw_constraints,
            path=forbidden.path,
            kind="forbidden_root",
            reason_code=forbidden.reason_code,
            evidence=forbidden.evidence,
        )
    for path, context in _path_mentions(text or ""):
        reason = _context_reason_for_path(context, path)
        if reason is None:
            continue
        kind, reason_code = reason
        if kind == "target_root" and not _path_is_grounded_in_workspace_context(
            path,
            workspace_context=workspace_context,
        ):
            continue
        _append_constraint(
            raw_constraints,
            path=path,
            kind=kind,
            reason_code=reason_code,
            evidence=context,
        )
    raw_constraints.extend(_leaf_name_constraints(text or "", workspace_context=workspace_context))
    return _constraints_from_items(raw_constraints)


def _constraints_from_items(items: list[PlanningPathConstraint]) -> PlanningScopeConstraints:
    forbidden = [item for item in items if item.kind == "forbidden_root"]
    decoy = [item for item in items if item.kind == "decoy_root"]
    unrelated = [item for item in items if item.kind == "unrelated_root"]
    blocked_keys = {_constraint_identity(item.path) for item in [*forbidden, *decoy, *unrelated]}
    target = [
        item
        for item in items
        if item.kind == "target_root" and _constraint_identity(item.path) not in blocked_keys
    ]
    return PlanningScopeConstraints(
        target_roots=_dedupe_constraints(target),
        forbidden_roots=_dedupe_constraints(forbidden),
        decoy_roots=_dedupe_constraints(decoy),
        unrelated_roots=_dedupe_constraints(unrelated),
    )


def planning_constraints_from_payload(raw: Any) -> PlanningScopeConstraints:
    if not isinstance(raw, dict):
        return PlanningScopeConstraints()
    items: list[PlanningPathConstraint] = []
    for key, kind in (
        ("target_roots", "target_root"),
        ("forbidden_roots", "forbidden_root"),
        ("decoy_roots", "decoy_root"),
        ("unrelated_roots", "unrelated_root"),
    ):
        for entry in raw.get(key) or []:
            if not isinstance(entry, dict):
                continue
            path = _normalize_constraint_path(str(entry.get("path") or ""))
            if not path:
                continue
            items.append(
                PlanningPathConstraint(
                    path=path,
                    kind=kind,  # type: ignore[arg-type]
                    reason_code=str(entry.get("reason_code") or kind).strip() or kind,
                    evidence=str(entry.get("evidence") or "").strip(),
                )
            )
    return _constraints_from_items(items)


def planning_constraints_from_plan(plan: dict[str, Any]) -> PlanningScopeConstraints:
    return planning_constraints_from_payload(plan.get(PLANNING_CONSTRAINTS_KEY))


def merge_planning_scope_constraints(
    *constraints: PlanningScopeConstraints,
    replace_target_roots: bool = False,
    unblock_target_roots: bool = False,
) -> PlanningScopeConstraints:
    items: list[PlanningPathConstraint] = []
    latest = constraints[-1] if constraints else PlanningScopeConstraints()
    latest_target_keys = (
        {_constraint_identity(item.path) for item in latest.target_roots}
        if unblock_target_roots
        else set()
    )
    for index, constraint_set in enumerate(constraints):
        is_latest = index == len(constraints) - 1
        if not replace_target_roots or is_latest:
            items.extend(constraint_set.target_roots)
        for blocked in [
            *constraint_set.forbidden_roots,
            *constraint_set.decoy_roots,
            *constraint_set.unrelated_roots,
        ]:
            if (
                unblock_target_roots
                and not is_latest
                and _constraint_identity(blocked.path) in latest_target_keys
            ):
                continue
            items.append(blocked)
    return _constraints_from_items(items)


def _latest_text_replaces_target_roots(
    *,
    text: str,
    extracted: PlanningScopeConstraints,
    direction_change: Any | None,
) -> bool:
    if not extracted.target_roots:
        return False
    if _ADDITIVE_TARGET_RE.search(text or ""):
        return False
    if direction_change is not None:
        return True
    return bool(_EXCLUSIVE_TARGET_RE.search(text or "") or _RETARGET_RE.search(text or ""))


def merge_latest_planning_scope_constraints(
    existing: PlanningScopeConstraints,
    *,
    text: str,
    workspace_context: dict[str, Any] | None = None,
    direction_change: Any | None = None,
) -> PlanningScopeConstraints:
    extracted = extract_planning_scope_constraints(text, workspace_context=workspace_context)
    return merge_planning_scope_constraints(
        existing,
        extracted,
        replace_target_roots=_latest_text_replaces_target_roots(
            text=text,
            extracted=extracted,
            direction_change=direction_change,
        ),
        unblock_target_roots=bool(extracted.target_roots),
    )


def update_plan_planning_constraints(
    plan: dict[str, Any],
    *,
    text: str,
    workspace_context: dict[str, Any] | None = None,
    direction_change: Any | None = None,
) -> tuple[PlanningScopeConstraints, bool]:
    existing = planning_constraints_from_plan(plan)
    merged = merge_latest_planning_scope_constraints(
        existing,
        text=text,
        workspace_context=workspace_context,
        direction_change=direction_change,
    )
    if not merged.has_constraints:
        return merged, False
    payload = merged.to_payload()
    if plan.get(PLANNING_CONSTRAINTS_KEY) == payload:
        return merged, False
    plan[PLANNING_CONSTRAINTS_KEY] = payload
    return merged, True


def planning_constraints_prompt_payload(
    constraints: PlanningScopeConstraints,
) -> dict[str, Any] | None:
    if not constraints.has_constraints:
        return None
    return constraints.to_payload()


def _has_glob(path: str) -> bool:
    return any(char in path for char in _GLOB_CHARS)


def _glob_prefix(path: str) -> str:
    prefix_chars: list[str] = []
    for char in path:
        if char in _GLOB_CHARS:
            break
        prefix_chars.append(char)
    return _constraint_root("".join(prefix_chars))


def path_matches_planning_root(path: str, root: str) -> bool:
    normalized_path = _normalize_constraint_path(path)
    normalized_root = _normalize_constraint_path(root)
    if not normalized_path or not normalized_root:
        return False
    if _has_glob(normalized_root):
        return fnmatchcase(normalized_path, normalized_root)
    if _has_glob(normalized_path):
        prefix = _glob_prefix(normalized_path)
        return bool(prefix) and (
            prefix.casefold() == normalized_root.casefold()
            or prefix.casefold().startswith(normalized_root.casefold() + "/")
        )
    path_key = normalized_path.casefold()
    root_key = normalized_root.casefold()
    return path_key == root_key or path_key.startswith(root_key + "/")


def path_within_target_roots(
    path: str,
    constraints: PlanningScopeConstraints,
) -> bool:
    if not constraints.target_roots:
        return True
    return any(path_matches_planning_root(path, item.path) for item in constraints.target_roots)


def _path_text_parts(path: str) -> set[str]:
    cleaned = _constraint_root(path).replace("\\", "/").casefold()
    parts: set[str] = set()
    for part in PurePosixPath(cleaned).parts:
        if not part or part == ".":
            continue
        stem = part.split(".", 1)[0]
        parts.add(part)
        if stem:
            parts.add(stem)
    return parts


def _target_labels(constraints: PlanningScopeConstraints) -> set[str]:
    labels: set[str] = set()
    for item in constraints.target_roots:
        labels.update(_path_text_parts(item.path))
    return labels


def _task_text(task: dict[str, Any]) -> str:
    acceptance = task.get("acceptance_criteria") or []
    acceptance_items = acceptance if isinstance(acceptance, list) else []
    return "\n".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            *(str(item or "") for item in acceptance_items),
        ]
    )


def _scope_entry_requires_target(path: str) -> bool:
    normalized = _constraint_root(path)
    if not normalized:
        return False
    if _NON_TARGET_SURFACE_RE.match(normalized):
        return False
    if is_code_implementation_path(normalized) or is_test_path(normalized):
        return True
    pure = PurePosixPath(normalized)
    name = pure.name.casefold()
    if name in {"package.json", "pyproject.toml", "cargo.toml", "go.mod", "pom.xml"}:
        return True
    if _has_glob(normalized):
        return not any(part.casefold() in {"docs", "doc", "notes"} for part in pure.parts)
    if "/" in normalized and "." not in pure.name:
        return True
    return False


def _has_explicit_target_evidence(
    *,
    path: str,
    task_text: str,
    constraints: PlanningScopeConstraints,
) -> bool:
    text = str(task_text or "")
    text_cf = text.casefold()
    labels = _target_labels(constraints)
    path_parts = _path_text_parts(path)
    label_match = bool(labels & path_parts) or any(label and label in text_cf for label in labels)
    if is_test_path(path):
        return label_match and _TEST_EVIDENCE_RE.search(text) is not None
    if is_code_implementation_path(path) or "/" in _constraint_root(path):
        return label_match and _SHARED_EVIDENCE_RE.search(text) is not None
    return label_match


def task_scope_constraint_violations(
    task: dict[str, Any],
    constraints: PlanningScopeConstraints,
) -> list[PlanningScopeViolation]:
    if not constraints.has_constraints:
        return []
    estimated_files, _dropped_estimated = split_normalized_repo_path_list(
        task.get("estimated_files")
    )
    write_scope, _dropped_write_scope = split_normalized_repo_path_list(task.get("write_scope"))
    paths = list(dict.fromkeys([*estimated_files, *write_scope]))
    if not paths:
        return []
    text = _task_text(task)
    violations: list[PlanningScopeViolation] = []
    for path in paths:
        for blocked in constraints.blocked_roots:
            if not path_matches_planning_root(path, blocked.path):
                continue
            violations.append(
                PlanningScopeViolation(
                    path=path,
                    classification=blocked.kind,
                    reason_code=blocked.reason_code,
                    evidence=blocked.evidence,
                    constraint_path=blocked.path,
                )
            )
            break
        else:
            if (
                constraints.target_roots
                and _scope_entry_requires_target(path)
                and not path_within_target_roots(path, constraints)
                and not _has_explicit_target_evidence(
                    path=path,
                    task_text=text,
                    constraints=constraints,
                )
            ):
                violations.append(
                    PlanningScopeViolation(
                        path=path,
                        classification="outside_target_root",
                        reason_code="invalid_out_of_scope",
                        evidence=(
                            "target roots: "
                            + ", ".join(item.path for item in constraints.target_roots)
                        ),
                    )
                )
    return violations


def task_has_target_root_scope(
    task: dict[str, Any],
    constraints: PlanningScopeConstraints,
) -> bool:
    if not constraints.target_roots:
        return False
    estimated_files, _dropped_estimated = split_normalized_repo_path_list(
        task.get("estimated_files")
    )
    write_scope, _dropped_write_scope = split_normalized_repo_path_list(task.get("write_scope"))
    return any(
        path_within_target_roots(path, constraints) for path in [*estimated_files, *write_scope]
    )


def filter_scope_entries_for_planning_constraints(
    paths: list[str],
    *,
    task: dict[str, Any],
    constraints: PlanningScopeConstraints,
) -> tuple[list[str], list[PlanningScopeViolation]]:
    if not constraints.has_constraints:
        return paths, []
    text_task = dict(task)
    text_task["estimated_files"] = list(paths)
    text_task["write_scope"] = list(paths)
    violations = task_scope_constraint_violations(text_task, constraints)
    violating = {_constraint_identity(violation.path) for violation in violations}
    kept = [path for path in paths if _constraint_identity(path) not in violating]
    return kept, violations
