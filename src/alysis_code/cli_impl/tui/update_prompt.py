"""TUI-native startup prompt for a newer Alysis Code release.

Shown at launch — before the first-run setup wizard and the guarded-workspace
picker — whenever the cached update check knows about a newer PyPI release the
user has not skipped or snoozed. Rendered as its own short-lived full-screen
``Application`` via the shared :func:`_run_option_picker`, so it matches the
visual grammar of the setup + workspace-guard screens.

Degrades gracefully: any terminal / prompt_toolkit failure returns
``(None, False)`` so the caller can fall back to the classic inline prompt,
exactly like the workspace-guard pickers do.
"""

from __future__ import annotations

from typing import Any

from .workspace_guard import _run_option_picker

UPDATE_ACTION_UPDATE = "update"
UPDATE_ACTION_LATER = "later"
UPDATE_ACTION_SKIP = "skip"


def select_update_action(
    *,
    current_version: str,
    latest_version: str,
    command: str | None = None,
    unsupported_reason: str | None = None,
    input: Any | None = None,
    output: Any | None = None,
) -> tuple[str | None, bool]:
    """Ask whether to apply a newer release now.

    Returns ``(action, interactive_available)`` where ``action`` is one of
    ``"update"`` / ``"later"`` / ``"skip"``, or ``None`` when the user cancels
    (Esc / Ctrl-C — treated as "later" by the caller). Focus starts on
    "Remind me later" so a reflexive Enter never runs an installer — matching
    the classic prompt's ``default="n"`` and the `alysis update` flow's
    ``typer.confirm(default=False)``.
    """
    if command:
        update_label = "Update now"
        update_desc = f"Runs: {command}"
    else:
        # No runnable command for this install: the row shows how to update
        # manually instead of promising an automatic one.
        update_label = "Show update instructions"
        update_desc = unsupported_reason or "Shows how to update this install."
    rows = [
        (UPDATE_ACTION_UPDATE, update_label, update_desc),
        (
            UPDATE_ACTION_LATER,
            "Remind me later",
            "Keep this version for now; ask again on a later launch.",
        ),
        (
            UPDATE_ACTION_SKIP,
            f"Skip version {latest_version}",
            "Don't ask again for this release.",
        ),
    ]
    return _run_option_picker(
        subtitle="update",
        title=f"Alysis Code {latest_version} is available — you have {current_version}",
        rows=rows,
        default_index=1,
        input=input,
        output=output,
    )


__all__ = [
    "UPDATE_ACTION_LATER",
    "UPDATE_ACTION_SKIP",
    "UPDATE_ACTION_UPDATE",
    "select_update_action",
]
