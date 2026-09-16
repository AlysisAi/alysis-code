"""Shared visibility rules for commands that only exist inside Forge."""

from __future__ import annotations

from collections.abc import Iterable

FORGE_SESSION_ONLY_COMMANDS = frozenset(
    {
        "/assistant",
        "/assets",
        "/back",
        "/done",
        "/execute",
        "/goal",
        "/plan",
        "/show",
        "/task",
    }
)


def forge_session_active(ui_mode: str) -> bool:
    """Return whether the command surface is currently inside Forge."""
    return str(ui_mode or "").strip().lower() == "forge"


def command_visible(command: str, *, ui_mode: str) -> bool:
    """Return whether ``command`` exists on the current chat surface."""
    token = str(command or "").strip().split(maxsplit=1)[0].lower()
    return token not in FORGE_SESSION_ONLY_COMMANDS or forge_session_active(ui_mode)


def visible_commands(commands: Iterable[str], *, ui_mode: str) -> list[str]:
    """Filter commands without changing their established order."""
    return [command for command in commands if command_visible(command, ui_mode=ui_mode)]
