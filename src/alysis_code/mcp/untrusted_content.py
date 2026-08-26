from __future__ import annotations

import copy
import re
from typing import Any

from ..safety.mcp_sanitize import sanitize_mcp_tool_description

MCP_UNTRUSTED_TEXT_CHAR_LIMIT = 12_000
_STRIPPED_SCHEMA_KEYS = frozenset(
    {
        "description",
        "markdownDescription",
        "title",
        "examples",
        "example",
        "default",
        "$comment",
    }
)
_BASE64ISH_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
_HEXISH_RE = re.compile(r"^[0-9A-Fa-f]+$")
_UNTRUSTED_TEXT_BODY_REPLACEMENTS = (
    ("[MCP_UNTRUSTED_TEXT]", "[MCP_UNTRUSTED_TEXT (server literal)]"),
    ("[/MCP_UNTRUSTED_TEXT]", "[/MCP_UNTRUSTED_TEXT (server literal)]"),
    (
        "--- BEGIN UNTRUSTED MCP TEXT ---",
        "--- BEGIN UNTRUSTED MCP TEXT (server literal) ---",
    ),
    (
        "--- END UNTRUSTED MCP TEXT ---",
        "--- END UNTRUSTED MCP TEXT (server literal) ---",
    ),
)


def build_host_owned_mcp_tool_description(
    *,
    server_id: str,
    tool_name: str,
    server_description: str | None = None,
) -> str:
    description = (
        f"External MCP tool from server '{server_id}' "
        f"(server tool name: '{tool_name}'). "
        "Server-authored descriptions are sanitized before prompt inclusion."
    )
    if server_description is None:
        return description
    return f"{description}\n{sanitize_mcp_tool_description(server_description)}"


def _looks_like_binary_blob(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if stripped.startswith("data:"):
        return True
    if len(stripped) >= 128 and len(stripped) % 4 == 0 and _BASE64ISH_RE.fullmatch(stripped):
        return True
    return len(stripped) >= 256 and _HEXISH_RE.fullmatch(stripped) is not None


def _neutralize_untrusted_text_body(text: str) -> str:
    body = str(text)
    # Prevent server-authored text from visually forging the host-owned wrapper fences.
    for raw_marker, escaped_marker in _UNTRUSTED_TEXT_BODY_REPLACEMENTS:
        body = body.replace(raw_marker, escaped_marker)
    return body


def build_untrusted_mcp_text_block(
    *,
    source_type: str,
    server_id: str,
    source_name: str,
    text: str,
    mime_type: str | None = None,
    original_char_count: int | None = None,
    truncated: bool = False,
) -> str:
    body = _neutralize_untrusted_text_body(text)
    char_count = original_char_count if original_char_count is not None else len(body)
    return "\n".join(
        [
            "[MCP_UNTRUSTED_TEXT]",
            f"source_type: {source_type}",
            f"server_id: {server_id}",
            f"source_name: {source_name}",
            f"mime_type: {mime_type or 'unknown'}",
            f"char_count: {char_count}",
            f"truncated: {'true' if truncated else 'false'}",
            "--- BEGIN UNTRUSTED MCP TEXT ---",
            body,
            "--- END UNTRUSTED MCP TEXT ---",
            "[/MCP_UNTRUSTED_TEXT]",
        ]
    )


def _reduce_model_facing_value(value: Any) -> Any:
    if isinstance(value, dict):
        reduced: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in _STRIPPED_SCHEMA_KEYS:
                continue
            if normalized_key in {"enum", "const"}:
                reduced[normalized_key] = copy.deepcopy(item)
            else:
                reduced[normalized_key] = _reduce_model_facing_value(item)
        return reduced
    if isinstance(value, list):
        return [_reduce_model_facing_value(item) for item in value]
    return value


def reduce_model_facing_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return _reduce_model_facing_value(schema)
