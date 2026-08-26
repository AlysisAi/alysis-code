"""Semantic Rich styles for terminal UI rendering."""

from __future__ import annotations

from typing import Literal

TerminalTheme = Literal["light", "dark", "neutral"]

STYLE_EMPHASIS = "bold"
STYLE_CONTENT = ""
STYLE_DIM = "dim"
STYLE_CHROME = "bright_black"
STYLE_SELECTED_LABEL = "bold"
STYLE_SELECTED_DESC = "bold"
STYLE_DESELECTED_LABEL = "dim"
STYLE_DESELECTED_DESC = "dim"
STYLE_ACCENT = "cyan"
STYLE_WARN = "yellow"
STYLE_ERROR = "red"
STYLE_SUCCESS = "green"
STYLE_SUBAGENT = "bright_cyan"

_BASE_STYLES: dict[str, str] = {
    "STYLE_EMPHASIS": STYLE_EMPHASIS,
    "STYLE_CONTENT": STYLE_CONTENT,
    "STYLE_DIM": STYLE_DIM,
    "STYLE_CHROME": STYLE_CHROME,
    "STYLE_SELECTED_LABEL": STYLE_SELECTED_LABEL,
    "STYLE_SELECTED_DESC": STYLE_SELECTED_DESC,
    "STYLE_DESELECTED_LABEL": STYLE_DESELECTED_LABEL,
    "STYLE_DESELECTED_DESC": STYLE_DESELECTED_DESC,
    "STYLE_WARN": STYLE_WARN,
    "STYLE_ERROR": STYLE_ERROR,
    "STYLE_SUCCESS": STYLE_SUCCESS,
    "STYLE_SUBAGENT": STYLE_SUBAGENT,
}


def theme_aware_styles(theme: TerminalTheme) -> dict[str, str]:
    """Return semantic styles whose accents are tuned for the detected theme."""
    styles = dict(_BASE_STYLES)
    if theme == "light":
        styles["STYLE_ACCENT"] = "blue"
    elif theme == "dark":
        styles["STYLE_ACCENT"] = STYLE_ACCENT
    else:
        styles["STYLE_ACCENT"] = ""
    return styles


__all__ = [
    "STYLE_ACCENT",
    "STYLE_CHROME",
    "STYLE_CONTENT",
    "STYLE_DESELECTED_DESC",
    "STYLE_DESELECTED_LABEL",
    "STYLE_DIM",
    "STYLE_EMPHASIS",
    "STYLE_ERROR",
    "STYLE_SELECTED_DESC",
    "STYLE_SELECTED_LABEL",
    "STYLE_SUBAGENT",
    "STYLE_SUCCESS",
    "STYLE_WARN",
    "TerminalTheme",
    "theme_aware_styles",
]
