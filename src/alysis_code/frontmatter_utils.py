from __future__ import annotations

import json
from typing import Any


def split_frontmatter(text: str) -> tuple[str | None, str]:
    if not text.startswith("---"):
        return None, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return None, text
    frontmatter = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    return frontmatter, body


def parse_frontmatter_yaml(
    frontmatter: str,
    *,
    allowed_keys: set[str] | None = None,
    list_fields: set[str] | None = None,
    string_fields: set[str] | None = None,
    bool_fields: set[str] | None = None,
) -> dict[str, Any]:
    allowed = set(allowed_keys or ())
    list_keys = set(list_fields or ())
    string_keys = set(string_fields or ())
    bool_keys = set(bool_fields or ())
    lines = frontmatter.splitlines()
    out: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if ":" not in raw_line:
            continue
        key_part, value_part = raw_line.split(":", 1)
        key = key_part.strip()
        if allowed and key not in allowed:
            continue
        value = value_part.strip()
        if key in list_keys:
            if value:
                out[key] = coerce_frontmatter_list(value)
                continue
            items: list[str] = []
            while i < len(lines):
                next_line = lines[i]
                stripped = next_line.strip()
                if not stripped:
                    i += 1
                    continue
                if not stripped.startswith("- "):
                    break
                items.append(stripped[2:].strip().strip('"').strip("'"))
                i += 1
            out[key] = [item for item in items if item]
            continue
        if key in bool_keys:
            lowered = value.lower()
            if lowered in {"true", "yes", "on", "1"}:
                out[key] = True
            elif lowered in {"false", "no", "off", "0"}:
                out[key] = False
            continue
        if key in string_keys or not allowed:
            out[key] = value.strip().strip('"').strip("'")
    return out


def coerce_frontmatter_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]
