# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from .cli_common import *
from ..chat.state import (
    _ChatExecutionRequest,
    _ChatPlanModeState,
    _ForgeChatEntrySelection,
    _ForgeChatState,
    _ForgeEnterCommand,
    _ForgePlannerSessionState,
)


def _chat_plan_mode_enabled(plan_mode_state: _ChatPlanModeState | None) -> bool:
    return bool(getattr(plan_mode_state, "enabled", False))


def _forge_enter_usage_lines() -> tuple[str, ...]:
    return (
        "[yellow]Usage:[/yellow] /forge",
        "                 /forge <goal>",
        "                 /forge resume",
        "[dim]/forge opens Forge. /forge <goal> opens Forge and sets the goal.[/dim]",
        "[dim]/forge resume reopens the current Forge run for this workspace.[/dim]",
    )


def _parse_forge_enter_command(*, cmd: str, arg: str) -> _ForgeEnterCommand | None:
    if cmd not in {"/forge", ":forge"}:
        return None
    normalized_arg = str(arg or "").strip()
    if not normalized_arg:
        return _ForgeEnterCommand(entry_mode="plain")
    lowered = normalized_arg.casefold()
    if lowered == "resume":
        return _ForgeEnterCommand(entry_mode="resume_pointer")
    # Planner-choice flags: the TUI intro popup asks "Use the planner?" natively and
    # submits one of these so Forge enters with the plan assistant forced on/off
    # (never treating the flag as a project goal). Also usable directly.
    if lowered in {"--planner", "planner"}:
        return _ForgeEnterCommand(entry_mode="plain", planner_assistant_default=True)
    if lowered in {"--no-planner", "no-planner"}:
        return _ForgeEnterCommand(entry_mode="plain", planner_assistant_default=False)
    return _ForgeEnterCommand(entry_mode="plain", initial_goal=normalized_arg)


def _consume_forge_entry_request_mode(*, forge_state: _ForgeChatState) -> str:
    raw_mode = str(getattr(forge_state, "entry_request_mode", "plain") or "").strip().lower()
    forge_state.entry_request_mode = "plain"
    if raw_mode == "resume_pointer":
        return raw_mode
    return "plain"


def _session_local_forge_resume_eligibility(
    *,
    forge_state: _ForgeChatState,
    workspace_binding: WorkspaceBinding,
) -> tuple[bool, str | None]:
    paths = forge_state.paths
    plan = forge_state.plan
    if paths is None or plan is None:
        return (False, None)

    stored_workspace_root = paths.root.resolve()
    current_workspace_root = workspace_binding.workspace_context.workspace_root.resolve()

    # Session-local Forge state is scoped to the canonical workspace root, not the
    # current focus path. Re-entering from another directory within the same workspace
    # keeps the same run; crossing to another workspace root forces a fresh run.
    if stored_workspace_root != current_workspace_root:
        return (False, "workspace_changed")
    return (True, None)


def _session_local_forge_focus_rebind_needed(
    *,
    paths: RunPaths,
    workspace_binding: WorkspaceBinding,
) -> bool:
    current_context = workspace_binding.workspace_context
    stored_focus_path = (paths.focus_path or paths.root).resolve()
    current_focus_path = current_context.focus_path.resolve()
    if stored_focus_path != current_focus_path:
        return True
    return (paths.focus_relpath or ".") != (current_context.focus_relpath or ".")


def _select_forge_chat_entry(
    *,
    forge_state: _ForgeChatState,
    workspace_binding: WorkspaceBinding,
) -> _ForgeChatEntrySelection:
    workspace_root = workspace_binding.workspace_context.workspace_root
    entry_mode = _consume_forge_entry_request_mode(forge_state=forge_state)
    if entry_mode == "resume_pointer":
        paths = load_current_run_paths(workspace_root)
        return _ForgeChatEntrySelection(
            paths=paths,
            plan=load_plan(paths),
            entry_kind="pointer_resume",
        )
    session_local_resume_allowed, session_local_resume_reason = (
        _session_local_forge_resume_eligibility(
            forge_state=forge_state,
            workspace_binding=workspace_binding,
        )
    )
    if session_local_resume_allowed:
        rebound_paths = rebind_run_paths_to_workspace_binding(
            paths=forge_state.paths,
            workspace_binding=workspace_binding,
        )
        return _ForgeChatEntrySelection(
            paths=rebound_paths,
            plan=forge_state.plan,
            entry_kind=(
                "session_local_resume_rebound"
                if _session_local_forge_focus_rebind_needed(
                    paths=forge_state.paths,
                    workspace_binding=workspace_binding,
                )
                else "session_local_resume"
            ),
        )
    paths = create_plan_run(workspace_root, workspace_binding=workspace_binding)
    return _ForgeChatEntrySelection(
        paths=paths,
        plan=load_plan(paths),
        entry_kind=(
            "fresh_workspace_changed"
            if session_local_resume_reason == "workspace_changed"
            else "fresh"
        ),
    )


def _forge_entry_status_text(*, entry_kind: str) -> str:
    if entry_kind == "session_local_resume":
        return "Resumed this chat session's Forge run in the current workspace."
    if entry_kind == "session_local_resume_rebound":
        return "Resumed this chat session's Forge run and rebound it to the current focus."
    if entry_kind == "pointer_resume":
        return "Resumed the current run pointer explicitly."
    if entry_kind == "fresh_workspace_changed":
        return "Started a fresh Forge run because this chat moved to a different workspace."
    return "Started a fresh Forge run for this chat session."


_CHAT_PROMPT_RESULT_PLAN_MODE_OFF = object()
_CHAT_ESCAPE_ACTION_PLAN_OFF = "plan_off"
_CHAT_ESCAPE_ACTION_PASTE_IMAGE = "paste_image"
_CHAT_ESCAPE_ACTION_NOOP = "noop"
_CHAT_PROMPT_ESCAPE_SEQUENCE_TIMEOUT_S = 1.0

__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
