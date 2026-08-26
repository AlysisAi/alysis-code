from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_MAX_REPORTED_TAGS = 16
_MAX_REPORTED_TAG_CHARS = 64

_ROLE_TAGS = frozenset({"assistant", "developer", "function", "system", "tool", "user"})
_HARNESS_TAGS = frozenset(
    {
        "asset_content",
        "asset_summary",
        "asset_text",
        "assistant_summary",
        "available_tool_catalog",
        "environment_context",
        "explicit_skill_context",
        "matched_skill_context",
        "ocr_text",
        "repo_conventions",
        "resume_context",
        "scoped_prompt_prelude",
        "skill_context",
        "skill_instructions",
        "subagent_context",
        "subagent_turn_context",
        "task_brief",
        "tool_call",
        "workspace_binding_context",
    }
)

_TAG_RE = re.compile(
    r"<\s*/?\s*(?P<name>[A-Za-z][A-Za-z0-9:_-]*)\b[^<>]*>",
    flags=re.IGNORECASE,
)
_ESCAPED_TAG_RE = re.compile(
    r"&lt;\s*/?\s*(?P<name>[A-Za-z][A-Za-z0-9:_-]*)\b.*?&gt;",
    flags=re.IGNORECASE,
)
_BLOCKED_INSTRUCTION_MARKER_RE = re.compile(
    r"\[blocked untrusted subagent "
    r"(?:instruction_override|permission_override|tool_demand)\]",
    flags=re.IGNORECASE,
)

_HIGH_RISK_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "instruction_override",
        (
            re.compile(
                r"\b(?:ignore|disregard|forget|override|bypass)\s+"
                r"(?:(?:all|any|the|your)\s+)?"
                r"(?:previous|prior|above|earlier|system|developer|parent)\s+"
                r"(?:instructions?|rules?|directives?|prompts?)\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:follow|obey|use)\s+(?:these|the\s+following|my|new)\s+"
                r"instructions?\s+(?:instead|now)\b",
                flags=re.IGNORECASE,
            ),
        ),
    ),
    (
        "permission_override",
        (
            re.compile(
                r"\b(?:grant|escalate|elevate|change|set|switch|override|enable|disable|bypass)\s+"
                r"(?:(?:the|your|parent|session)\s+)?"
                r"(?:permissions?|permission\s+mode|sandbox(?:\s+mode)?|approval(?:\s+mode)?)"
                r"(?:\s+(?:to|as)\s+[A-Za-z0-9_-]+)?\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:you|the\s+parent|parent|assistant|main\s+agent)\s+(?:now\s+)?"
                r"(?:have|has)\s+(?:full|unrestricted|elevated)\s+"
                r"(?:access|permissions?)\b",
                flags=re.IGNORECASE,
            ),
        ),
    ),
    (
        "tool_demand",
        (
            re.compile(
                r"\b(?:you|the\s+parent|parent|assistant|main\s+agent)\s+"
                r"(?:must|should|shall|need(?:s)?\s+to)\s+(?:now\s+)?"
                r"(?:call|run|invoke|use|execute)\s+(?:the\s+)?"
                r"(?:[A-Za-z][A-Za-z0-9.-]*_[A-Za-z0-9_.-]+|"
                r"[A-Za-z][A-Za-z0-9_.-]*\s+tool)\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:call|run|invoke|use|execute)\s+(?:the\s+)?"
                r"(?:[A-Za-z][A-Za-z0-9.-]*_[A-Za-z0-9_.-]+|"
                r"[A-Za-z][A-Za-z0-9_.-]*\s+tool)\s+(?:now|instead)\b",
                flags=re.IGNORECASE,
            ),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class SanitizedSubagentReport:
    text: str
    sanitized: bool
    detected_categories: tuple[str, ...] = ()
    detected_tags: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "sanitized": self.sanitized,
            "detected_categories": list(self.detected_categories),
            "detected_tags": list(self.detected_tags),
        }


def _tag_category(tag_name: str) -> str | None:
    normalized = tag_name.casefold()
    if normalized in _ROLE_TAGS:
        return "role_tag"
    if (
        normalized in _HARNESS_TAGS
        or normalized.endswith("_context")
        or normalized.startswith("alysis_")
    ):
        return "harness_tag"
    return None


def sanitize_subagent_report(text: str) -> SanitizedSubagentReport:
    original = str(text or "")
    categories: set[str] = set()
    tags: set[str] = set()

    def _escape_tag(match: re.Match[str]) -> str:
        tag_name = str(match.group("name") or "")
        category = _tag_category(tag_name)
        if category is None:
            return match.group(0)
        categories.add(category)
        tags.add(tag_name.casefold()[:_MAX_REPORTED_TAG_CHARS])
        return match.group(0).replace("<", "&lt;").replace(">", "&gt;")

    sanitized_text = _TAG_RE.sub(_escape_tag, original)
    for category, patterns in _HIGH_RISK_PATTERNS:
        for pattern in patterns:
            sanitized_text, replacements = pattern.subn(
                f"[blocked untrusted subagent {category}]",
                sanitized_text,
            )
            if replacements:
                categories.add(category)

    ordered_categories = tuple(
        category
        for category in (
            "role_tag",
            "harness_tag",
            "instruction_override",
            "permission_override",
            "tool_demand",
        )
        if category in categories
    )
    ordered_tags = tuple(sorted(tags)[:_MAX_REPORTED_TAGS])
    return SanitizedSubagentReport(
        text=sanitized_text,
        sanitized=sanitized_text != original,
        detected_categories=ordered_categories,
        detected_tags=ordered_tags,
    )


def subagent_report_evidence_text(text: str) -> str:
    def _remove_protected_wrapper(match: re.Match[str]) -> str:
        return " " if _tag_category(str(match.group("name") or "")) is not None else match.group(0)

    without_wrappers = _ESCAPED_TAG_RE.sub(_remove_protected_wrapper, str(text or ""))
    return _BLOCKED_INSTRUCTION_MARKER_RE.sub(" ", without_wrappers)
