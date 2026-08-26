from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DirectionChange:
    old_terms: tuple[str, ...]
    new_terms: tuple[str, ...]
    reason: str
    raw_text: str


_TERM_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CLAUSE_END_RE = r"(?=[;,\n]|(?:\.\s)|\.$|$)"
_DIRECTION_BOUNDARY_RE = (
    r"(?=(?:\s+entirely|\s+from\b|\s+and\s+use\b|\s+use\b|\s+any\s+more|"
    r"\s+anymore|\s+instead\b|[;,\n]|(?:\.\s)|\.$|$))"
)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "anymore",
        "approach",
        "entirely",
        "from",
        "more",
        "old",
        "plan",
        "previous",
        "the",
        "to",
        "use",
        "using",
        "with",
        "work",
    }
)
_GENERIC_MATCH_TOKENS = frozenset(
    {
        "config",
        "configuration",
        "file",
        "flag",
        "implementation",
        "setting",
        "settings",
        "task",
        "tasks",
    }
)

_DIRECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "replace",
        re.compile(
            rf"\breplace\s+(?:the\s+)?(?P<old>.+?)\s+with\s+(?P<new>.+?){_CLAUSE_END_RE}",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "switch",
        re.compile(
            rf"\bswitch\s+from\s+(?:the\s+)?(?P<old>.+?)\s+to\s+(?P<new>.+?){_CLAUSE_END_RE}",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "instead_of",
        re.compile(
            rf"\b(?:use|using)\s+(?P<new>.+?)\s+instead\s+of\s+(?:the\s+)?(?P<old>.+?){_CLAUSE_END_RE}",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "drop",
        re.compile(
            rf"\b(?:drop|remove|abandon|forget)\s+(?:the\s+)?(?P<old>.+?){_DIRECTION_BOUNDARY_RE}",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "do_not_implement",
        re.compile(
            rf"\bdo\s+not\s+(?:implement|use|build|add|create)\s+(?:the\s+)?(?P<old>.+?){_DIRECTION_BOUNDARY_RE}",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "no_longer",
        re.compile(
            r"\bno\s+(?P<old>[a-z0-9_ .+\-/]{2,120}?)\s+(?:anymore|any\s+more)\b",
            re.IGNORECASE,
        ),
    ),
)
_USE_INSTEAD_RE = re.compile(
    r"\b(?:use|using)\s+(?P<new>.+?)\s+instead\b",
    re.IGNORECASE | re.DOTALL,
)
_NEGATED_DROP_PREFIX_RE = re.compile(
    r"(?:^|[\s.;:,])(?:do\s+not|don't|dont|never|must\s+not|should\s+not|without|not)\s+$",
    re.IGNORECASE,
)


def _dedupe_keep_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = value.strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return tuple(out)


def _clean_term(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("`", " ").replace('"', " ")).strip()
    text = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s+(?:from\s+the\s+plan|from\s+plan|any\s+more|anymore|entirely|instead)$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return text


def _tokens(value: str) -> tuple[str, ...]:
    normalized = str(value or "").casefold().replace("-", " ").replace("/", " ")
    tokens: list[str] = []
    for token in _TERM_TOKEN_RE.findall(normalized):
        if token in _STOPWORDS:
            continue
        tokens.append(token)
        if token == "env":
            tokens.append("environment")
        elif token == "environment":
            tokens.append("env")
        elif token == "var":
            tokens.append("variable")
        elif token == "variable":
            tokens.append("var")
    return _dedupe_keep_order(tokens)


def _term_groups(terms: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    for term in terms:
        tokens = _tokens(term)
        if tokens:
            groups.append(tokens)
    return tuple(groups)


def _group_matches(tokens: set[str], group: tuple[str, ...]) -> bool:
    if not group:
        return False
    rare_tokens = [token for token in group if token not in _GENERIC_MATCH_TOKENS]
    if rare_tokens:
        return any(token in tokens for token in rare_tokens)
    return all(token in tokens for token in group)


def _text_tokens(text: str) -> set[str]:
    return set(_tokens(text))


def _extract_new_terms(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in _USE_INSTEAD_RE.finditer(text or ""):
        cleaned = _clean_term(match.group("new"))
        if cleaned:
            found.append(cleaned)
    return _dedupe_keep_order(found)


def detect_direction_change(text: str) -> DirectionChange | None:
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    old_terms: list[str] = []
    new_terms: list[str] = []
    reasons: list[str] = []
    for reason, pattern in _DIRECTION_PATTERNS:
        for match in pattern.finditer(raw_text):
            if reason == "drop" and _match_has_negated_drop_prefix(raw_text, match.start()):
                continue
            old = _clean_term(match.groupdict().get("old", ""))
            if not old:
                continue
            old_terms.append(old)
            reasons.append(reason)
            new = _clean_term(match.groupdict().get("new", ""))
            if new:
                new_terms.append(new)

    new_terms.extend(_extract_new_terms(raw_text))
    deduped_old = _dedupe_keep_order(old_terms)
    if not deduped_old:
        return None
    return DirectionChange(
        old_terms=deduped_old,
        new_terms=_dedupe_keep_order(new_terms),
        reason=", ".join(_dedupe_keep_order(reasons)) or "direction_change",
        raw_text=raw_text,
    )


def _match_has_negated_drop_prefix(text: str, start: int) -> bool:
    prefix = str(text or "")[: max(0, start)]
    return _NEGATED_DROP_PREFIX_RE.search(prefix[-40:]) is not None


def direction_change_to_record(change: DirectionChange) -> dict[str, Any]:
    return {
        "type": "direction_change",
        "reason": change.reason,
        "old_terms": list(change.old_terms),
        "new_terms": list(change.new_terms),
        "user_text": change.raw_text,
    }


def direction_change_from_record(record: Any) -> DirectionChange | None:
    if not isinstance(record, dict):
        return None
    old_terms = _dedupe_keep_order([str(item) for item in record.get("old_terms") or []])
    if not old_terms:
        return None
    new_terms = _dedupe_keep_order([str(item) for item in record.get("new_terms") or []])
    return DirectionChange(
        old_terms=old_terms,
        new_terms=new_terms,
        reason=str(record.get("reason") or "direction_change").strip() or "direction_change",
        raw_text=str(record.get("user_text") or "").strip(),
    )


def text_matches_obsolete_direction(text: str, change: DirectionChange) -> bool:
    tokens = _text_tokens(text)
    if not tokens:
        return False
    old_match = any(_group_matches(tokens, group) for group in _term_groups(change.old_terms))
    if not old_match:
        return False
    new_match = any(_group_matches(tokens, group) for group in _term_groups(change.new_terms))
    return not new_match


def _direction_changes_for_path_filter(
    *,
    latest_user_text: str = "",
    task_text: str = "",
) -> tuple[DirectionChange, ...]:
    changes: list[DirectionChange] = []
    for text in (latest_user_text, task_text):
        change = detect_direction_change(text)
        if change is not None and change not in changes:
            changes.append(change)
    return tuple(changes)


def filter_obsolete_direction_paths(
    paths: list[str],
    *,
    latest_user_text: str = "",
    task_text: str = "",
) -> tuple[list[str], list[str]]:
    changes = _direction_changes_for_path_filter(
        latest_user_text=latest_user_text,
        task_text=task_text,
    )
    if not changes:
        return paths, []

    kept: list[str] = []
    dropped: list[str] = []
    for path in paths:
        if any(text_matches_obsolete_direction(path, change) for change in changes):
            dropped.append(path)
            continue
        kept.append(path)
    return kept, dropped
