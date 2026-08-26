from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_REPLY_LANGUAGE = "English"
DEFAULT_REPLY_SCRIPT = "Latin"

_MAX_LANGUAGE_FIELD_CHARS = 80


@dataclass(frozen=True)
class LanguagePolicyDecision:
    language: str = ""
    script: str = ""
    confidence: float = 0.0
    explicit_language_override: bool = False
    source: str = "default"
    failure_reason: str = ""


def _normalize_model_text(raw: object, *, max_chars: int = _MAX_LANGUAGE_FIELD_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    if not text:
        return ""
    return text[:max_chars]


def normalize_language_name(raw: object) -> str:
    return _normalize_model_text(raw)


def normalize_script_name(raw: object) -> str:
    return _normalize_model_text(raw)
