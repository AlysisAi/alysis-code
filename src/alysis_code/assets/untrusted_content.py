from __future__ import annotations

_ASSET_UNTRUSTED_REPLACEMENTS = (
    ("[ASSET_UNTRUSTED_CONTENT]", "[ASSET_UNTRUSTED_CONTENT (asset literal)]"),
    ("[/ASSET_UNTRUSTED_CONTENT]", "[/ASSET_UNTRUSTED_CONTENT (asset literal)]"),
    (
        "--- BEGIN UNTRUSTED ASSET CONTENT ---",
        "--- BEGIN UNTRUSTED ASSET CONTENT (asset literal) ---",
    ),
    (
        "--- END UNTRUSTED ASSET CONTENT ---",
        "--- END UNTRUSTED ASSET CONTENT (asset literal) ---",
    ),
    ("</asset_content>", "</asset_content (asset literal)>"),
    ("</asset_summary>", "</asset_summary (asset literal)>"),
)


def build_untrusted_asset_text_block(
    *,
    asset_id: str,
    text: str,
    mime_type: str | None = None,
    original_char_count: int | None = None,
    truncated: bool = False,
) -> str:
    body = _neutralize_asset_text_body(text)
    char_count = original_char_count if original_char_count is not None else len(body)
    return "\n".join(
        [
            "[ASSET_UNTRUSTED_CONTENT]",
            f"asset_id: {asset_id}",
            f"mime_type: {mime_type or 'unknown'}",
            f"char_count: {char_count}",
            f"truncated: {'true' if truncated else 'false'}",
            "--- BEGIN UNTRUSTED ASSET CONTENT ---",
            body,
            "--- END UNTRUSTED ASSET CONTENT ---",
            "[/ASSET_UNTRUSTED_CONTENT]",
        ]
    )


def _neutralize_asset_text_body(text: str) -> str:
    body = str(text)
    for raw_marker, escaped_marker in _ASSET_UNTRUSTED_REPLACEMENTS:
        body = body.replace(raw_marker, escaped_marker)
    return body
