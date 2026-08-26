"""Static text + the model-name prettifier for the TUI welcome/footer.

Kept as pure functions so they can be unit-tested without constructing the
prompt_toolkit application.
"""

from __future__ import annotations

import re

from prompt_toolkit.formatted_text import FormattedText

HEADING_TEXT = "Alysis Code"  # plain wordmark fallback (narrow terminals)
CREDIT_TEXT = "crafted by AlysisAI"
HINT_TEXT = "/forge for an autonomous run  ·  tab to switch persona  ·  /help for everything"
# Welcome screen: full greeting. Once the conversation is underway the input
# switches to the shorter follow-up placeholder below.
INPUT_PLACEHOLDER = "I'm Alysis Code, your coding buddy — how can I help you?"
INPUT_PLACEHOLDER_FOLLOWUP = "Message Alysis Code…"
# Forge planning session: nudge the forge verbs (and that plain text becomes a
# requirement) so the input reads as the plan editor it now is.
INPUT_PLACEHOLDER_FORGE = "Forge — /goal · /task · /show · /execute plan · /done"

# Tokens that should keep a specific casing instead of naive title-casing.
_ACRONYMS = {
    "ai": "AI",
    "deepseek": "DeepSeek",
    "glm": "GLM",
    "gpt": "GPT",
    "llm": "LLM",
    "mimo": "MiMo",
    "openai": "OpenAI",
    "qwen": "Qwen",
    "xai": "xAI",
}


def pretty_model_label(model: str | None) -> str:
    """Turn a raw model id into a friendly footer label.

    ``deepseek-chat`` -> ``DeepSeek Chat``; ``gpt-4o`` -> ``GPT 4o``. Falls back
    to the raw id when there is nothing sensible to do.
    """
    raw = (model or "").strip()
    if not raw:
        return "model"
    name = raw.rsplit("/", 1)[-1]
    tokens = [t for t in re.split(r"[-_\s]+", name) if t]
    out: list[str] = []
    for token in tokens:
        low = token.lower()
        if low in _ACRONYMS:
            out.append(_ACRONYMS[low])
        elif token.isupper():
            out.append(token)
        else:
            out.append(token[:1].upper() + token[1:])
    return " ".join(out) or raw


def heading_fragments() -> FormattedText:
    return FormattedText([("class:tui.heading", HEADING_TEXT)])


def hint_fragments() -> FormattedText:
    return FormattedText([("class:tui.hint", HINT_TEXT)])


__all__ = [
    "HEADING_TEXT",
    "HINT_TEXT",
    "INPUT_PLACEHOLDER",
    "INPUT_PLACEHOLDER_FOLLOWUP",
    "INPUT_PLACEHOLDER_FORGE",
    "heading_fragments",
    "hint_fragments",
    "pretty_model_label",
]
