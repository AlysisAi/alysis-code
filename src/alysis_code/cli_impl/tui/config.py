"""Feature-flag detection for the full-screen TUI.

The TUI is the default interactive chat surface. Set ``ALYSIS_TUI=0`` to
temporarily fall back to the classic terminal chat.
"""

from __future__ import annotations

from ...branding import env_get

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _env_flag(name: str) -> bool | None:
    raw = env_get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSEY:
        return False
    return None


def is_tui_enabled() -> bool:
    """Return True when the full-screen TUI should be used."""
    return _env_flag("ALYSIS_TUI") is not False


__all__ = ["is_tui_enabled"]
