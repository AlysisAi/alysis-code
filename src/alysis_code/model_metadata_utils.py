from __future__ import annotations

import re
from urllib.parse import urlparse

_DATE_SUFFIX_RE = re.compile(r"^(?P<base>.+)-\d{4}-\d{2}-\d{2}$")


def parse_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            if not value.is_integer():
                return None
            parsed = int(value)
        else:
            text = str(value).strip()
            try:
                parsed = int(text)
            except ValueError:
                as_float = float(text)
                if not as_float.is_integer():
                    return None
                parsed = int(as_float)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def parse_non_negative_float(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def model_name_variants(model_name: str) -> list[str]:
    clean = model_name.strip()
    if not clean:
        return []

    variants: list[str] = [clean]
    if "/" in clean:
        provider_stripped = clean.split("/")[-1].strip()
        if provider_stripped:
            variants.append(provider_stripped)

    for candidate in list(variants):
        match = _DATE_SUFFIX_RE.match(candidate)
        if match is None:
            continue
        base = match.group("base").strip()
        if base:
            variants.append(base)

    out: list[str] = []
    seen: set[str] = set()
    for candidate in variants:
        folded = candidate.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(candidate)
    return out


def normalize_base_url(value: str) -> str:
    clean = value.strip().rstrip("/")
    if not clean:
        return ""
    try:
        parsed = urlparse(clean)
    except ValueError:
        return clean
    if not parsed.scheme or not parsed.netloc:
        return clean
    normalized = parsed._replace(
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
        params="",
        query="",
        fragment="",
    )
    return normalized.geturl().rstrip("/")
