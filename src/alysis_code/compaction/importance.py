from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from alysis_code.token_budget import estimate_tokens

_WS_RE = re.compile(r"\s+")
_REQUIREMENT_RE = re.compile(
    r"\b(must|do not|never|should|acceptance criteria|requirement|constraint|scope)\b",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(
    r"\b(traceback|exception|error|failed|failure|stack trace|non-zero exit|exit code)\b",
    re.IGNORECASE,
)
_VERIFY_RE = re.compile(
    r"\b(pytest|ruff|npm test|pnpm test|cargo test|go test|mvn test)\b",
    re.IGNORECASE,
)
_PATH_RE = re.compile(
    r"\b([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|[A-Za-z0-9_.-]+\.(py|md|toml|json|yaml|yml|sh|ts|tsx|js|jsx|rs|go))\b"
)
_DIFF_RE = re.compile(r"diff --git|@@\s+-\d", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```")
_COMMAND_RE = re.compile(
    r"\b(python3?\s+-m|pip\s+install|git\s+(status|diff|apply|commit|merge)|docker\s+run|bwrap\b)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScoredTurn:
    start: int
    end: int
    token_estimate: int
    score: float
    density: float
    reasons: list[str]
    user_preview: str


def _normalize_preview(text: str, *, limit: int = 200) -> str:
    compact = _WS_RE.sub(" ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def extract_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                part_type = str(part.get("type") or "")
                if part_type == "text":
                    parts.append(str(part.get("text") or ""))
                elif part_type == "image_url":
                    parts.append("<image>")
                else:
                    parts.append(str(part))
            else:
                parts.append(str(part))
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def score_text(text: str) -> tuple[float, list[str]]:
    if not text.strip():
        return 0.0, []

    score = 0.0
    reasons: list[str] = []
    lowered = text.lower()

    req_hits = len(_REQUIREMENT_RE.findall(text))
    if req_hits > 0:
        score += min(4.0, 1.2 * req_hits)
        reasons.append("requirements_or_constraints")

    err_hits = len(_ERROR_RE.findall(text))
    if err_hits > 0:
        score += min(4.5, 1.5 * err_hits)
        reasons.append("errors_or_failures")

    verify_hits = len(_VERIFY_RE.findall(text))
    if verify_hits > 0:
        score += min(2.0, 0.8 * verify_hits)
        reasons.append("verification_commands")

    if _DIFF_RE.search(text):
        score += 1.8
        reasons.append("diff_or_patch")

    path_hits = len(_PATH_RE.findall(text))
    if path_hits > 0:
        score += min(2.5, 0.4 * path_hits)
        reasons.append("file_paths")

    if _CODE_FENCE_RE.search(text):
        score += 1.0
        reasons.append("code_block")

    command_hits = len(_COMMAND_RE.findall(text))
    if command_hits > 0:
        score += min(2.5, 0.6 * command_hits)
        reasons.append("shell_or_git_commands")

    if "acceptance criteria" in lowered:
        score += 1.5
        reasons.append("acceptance_criteria")

    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        key = reason.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(reason)
    return score, deduped


def estimate_turn_tokens(turn_messages: list[dict[str, Any]]) -> int:
    from alysis_code.compaction.conversation_compactor import (
        sanitize_messages_for_estimation,
    )

    payload = json.dumps(
        sanitize_messages_for_estimation(turn_messages),
        ensure_ascii=False,
        sort_keys=True,
    )
    return max(1, estimate_tokens(payload))


def score_turn(turn_messages: list[dict[str, Any]]) -> tuple[float, list[str], str]:
    score = 0.0
    reasons: list[str] = []
    user_preview = ""

    for msg in turn_messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        if role == "user":
            score += 2.0
        elif role == "assistant":
            score += 1.0
        elif role == "tool":
            score += 0.5

        text = extract_text(msg)
        text_score, text_reasons = score_text(text)
        score += text_score
        reasons.extend(text_reasons)

        if not user_preview and role == "user":
            user_preview = _normalize_preview(text)

    deduped: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        key = reason.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(reason)

    return score, deduped, user_preview
