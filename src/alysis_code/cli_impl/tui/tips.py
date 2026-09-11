"""Usage tips shown while an agent turn is running."""

from __future__ import annotations

from dataclasses import dataclass

from ...branding import PROJECT_SOURCE_URL


@dataclass(frozen=True)
class WorkingTip:
    text: str
    url: str | None = None


TIP_INTERVAL_SECONDS = 15
WORKING_TIPS: tuple[WorkingTip, ...] = (
    WorkingTip("Use /trace to set reasoning and tool detail: off, compact, or full."),
    WorkingTip("Use /permissions to choose when the agent asks for approval."),
    WorkingTip("Use /context to inspect context usage and the space remaining."),
    WorkingTip("Use /usage to check tokens and cost for this session."),
    WorkingTip("Use /help to explore commands without stopping the current task."),
    WorkingTip("Use /subagents to inspect delegated work while it runs."),
    WorkingTip("Press Ctrl+J to add a new line to your message."),
    WorkingTip("Name relevant files or folders to help the agent find the right code."),
    WorkingTip("Describe the expected result and how to verify it."),
    WorkingTip("Use /config to browse settings; saved changes apply after this turn."),
    WorkingTip("Star us on GitHub!", url=PROJECT_SOURCE_URL),
)


def working_tip(elapsed_seconds: float, *, turn_index: int = 0) -> WorkingTip:
    """Rotate slowly during a turn and vary the first tip across turns."""
    index = turn_index + int(max(0.0, elapsed_seconds) // TIP_INTERVAL_SECONDS)
    return WORKING_TIPS[index % len(WORKING_TIPS)]
