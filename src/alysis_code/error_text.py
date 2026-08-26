from __future__ import annotations

import re

from .llm.metadata import sanitize_urls_for_output

_SENSITIVE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED]"),
    (
        re.compile(r"(Bearer\s+)[^\s,;}\"']{8,}", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(Authorization\s*:\s*)(.+)", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"((?:[\"']?(?:api_key|api-key|x-api-key|x-goog-api-key)[\"']?"
            r"\s*[:=]\s*[\"']?))([^\"'\s,;&}]+)([\"']?)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]\3",
    ),
    (
        re.compile(
            r"((?:ALYSIS_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY)"
            r"\s*[:=]\s*)([^\s,;&]+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
)
_DEFAULT_MAX_ERROR_SUMMARY_CHARS = 320


def redact_sensitive_error_text(text: str) -> str:
    out = str(text or "")
    for pattern, replacement in _SENSITIVE_REPLACEMENTS:
        out = pattern.sub(replacement, out)
    return out


def sanitize_error_text_for_output(text: object) -> str:
    """Redact credentials and private URL route material from error text."""

    return redact_sensitive_error_text(sanitize_urls_for_output(text))


def sanitize_error_summary(
    text: str,
    *,
    max_chars: int = _DEFAULT_MAX_ERROR_SUMMARY_CHARS,
) -> str:
    clean = sanitize_error_text_for_output(" ".join(str(text or "").split())).strip()
    if not clean:
        return "No additional error details."
    if len(clean) <= max_chars:
        return clean
    if max_chars <= 15:
        return clean[:max_chars]
    return clean[: max_chars - 15].rstrip() + "...(truncated)"


def sanitize_optional_error_summary(
    text: str | None,
    *,
    max_chars: int = _DEFAULT_MAX_ERROR_SUMMARY_CHARS,
) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    return sanitize_error_summary(raw, max_chars=max_chars)
