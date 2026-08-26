from __future__ import annotations

import re

from .models import SkillBundle, SkillMatch

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./+-]*", re.IGNORECASE)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "build",
    "do",
    "for",
    "help",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "please",
    "the",
    "this",
    "to",
    "we",
    "with",
}


def match_skills(
    request: str,
    *,
    skills: list[SkillBundle] | tuple[SkillBundle, ...],
    max_results: int = 5,
) -> tuple[SkillMatch, ...]:
    request_text = str(request or "").strip()
    if not request_text:
        return ()
    request_tokens = _tokenize(request_text)
    if not request_tokens:
        return ()
    matches: list[SkillMatch] = []
    request_lower = request_text.casefold()
    for skill in skills:
        name_tokens = _tokenize(skill.name)
        description_tokens = _tokenize(skill.description)
        score = 0
        matched_terms: list[str] = []
        if skill.name.casefold() in request_lower or skill.bundle_name.casefold() in request_lower:
            score += 12
            matched_terms.append(skill.name)
        overlap = sorted(request_tokens & name_tokens)
        if overlap:
            score += len(overlap) * 5
            matched_terms.extend(overlap)
        desc_overlap = sorted(request_tokens & description_tokens)
        if desc_overlap:
            score += len(desc_overlap) * 2
            matched_terms.extend(desc_overlap)
        if score <= 0:
            continue
        matches.append(
            SkillMatch(
                skill=skill,
                score=score,
                matched_terms=tuple(_ordered_unique(matched_terms)),
            )
        )
    matches.sort(
        key=lambda item: (
            -item.score,
            item.skill.name.casefold(),
            item.skill.bundle_path.as_posix(),
        )
    )
    return tuple(matches[: max(0, max_results)])


def _tokenize(text: str) -> set[str]:
    tokens = {
        token.casefold()
        for token in _TOKEN_RE.findall(str(text or ""))
        if token and token.casefold() not in _STOPWORDS
    }
    return {token for token in tokens if len(token) > 1}


def _ordered_unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        lowered = value.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(value)
    return out
