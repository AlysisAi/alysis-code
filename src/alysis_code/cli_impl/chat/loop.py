# ruff: noqa: F821
# Dependencies are injected at runtime from alysis_code.cli to preserve monkeypatch surfaces.
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import typer

from ...branding import env_get
from ...compaction.conversation_compactor import CompactionState
from ...error_text import sanitize_error_text_for_output
from ...failure_category import exit_code_for_failure
from ...personas import (
    DEFAULT_PERSONA,
    get_persona,
    is_persona_name,
    next_persona,
    normalize_persona,
    persona_modes_enabled,
    persona_overlay_messages,
    persona_overlay_user_messages,
    resolve_persona_exec_mode,
)
from ...run_outcome import INFRASTRUCTURE_FAILURE_EXIT_CODE
from ...runtime_kind import RuntimeKind
from ...surface.console import safe_plain_error
from .state import _ChatExecutionRequest, _ChatPlanModeState, _ForgeChatState

_PROTECTED_GLOBAL_NAMES: set[str] = set()
_PATCHABLE_DEPENDENCY_GLOBAL_NAMES: set[str] = {
    "create_session",
    "load_config",
    "_chat_trace_level",
    "_current_branch_label",
    "_guarded_workspace_prompt_text",
    "_is_git_dirty",
    "_is_narrow_terminal",
    "_is_non_interactive_terminal",
    "_maybe_make_chat_prompt_session",
    "_run_inline_option_selector",
    "_select_guarded_workspace_action_interactive",
    "paste_clipboard_image",
    "typer",
}


def _sync_cli_globals(cli_mod: Any) -> None:
    module_globals = globals()
    if not _PROTECTED_GLOBAL_NAMES:
        for local_name, local_value in module_globals.items():
            if callable(local_value):
                _PROTECTED_GLOBAL_NAMES.add(local_name)
    for name, value in cli_mod.__dict__.items():
        if name.startswith("__"):
            continue
        if name in _PROTECTED_GLOBAL_NAMES and name not in _PATCHABLE_DEPENDENCY_GLOBAL_NAMES:
            continue
        if (
            name in _PROTECTED_GLOBAL_NAMES
            and module_globals.get(name) is value
            and name in _PATCHABLE_DEPENDENCY_GLOBAL_NAMES
        ):
            continue
        module_globals[name] = value


def _path_binding_source(path_source: Any, path: Any) -> str:
    if path_source is not None and path_source is not ParameterSource.DEFAULT:
        return "explicit_path"
    if path not in (None, ".", Path(".")):
        return "explicit_path"
    return "cwd"


_RAW_BENCHMARK_PROFILE_NAMES: set[str] = {
    "benchmark",
    "bench",
    "raw-agent",
    "raw_agent",
    "raw-benchmark",
    "raw_benchmark",
    "raw-agent-benchmark",
    "raw_agent_benchmark",
}
_RAW_BENCHMARK_MIN_MAX_STEPS = 80


def _raw_benchmark_profile_requested(*, benchmark: bool) -> bool:
    if benchmark:
        return True
    profile = env_get("ALYSIS_RUN_PROFILE", "") or ""
    normalized = profile.strip().lower()
    return normalized in _RAW_BENCHMARK_PROFILE_NAMES


def _apply_raw_benchmark_profile(cfg: Any) -> None:
    cfg.default_mode = "auto"
    cfg.routing_mode = "code_only"
    cfg.subagents_enabled = False
    cfg.skills_enabled = False
    cfg.skills_auto_invoke = False
    cfg.custom_tools_enabled = False
    cfg.web_search_mode = "off"
    cfg.max_steps = max(int(getattr(cfg, "max_steps", 0) or 0), _RAW_BENCHMARK_MIN_MAX_STEPS)


_CHAT_WORKDIR_NAVIGATION_PREFIXES: tuple[str, ...] = (
    "from now on operate under ",
    "from now on operate in ",
    "from now on work under ",
    "from now on work in ",
    "switch to ",
    "work under ",
    "work in ",
    "operate under ",
    "operate in ",
    "go to ",
)
_CHAT_WORKDIR_TARGET_RE = re.compile(
    r"^(?P<path>\"[^\"]+\"|'[^']+'|[^,\s]+(?:[\\/][^,\s]+)*)(?:\s*(?:,?\s*(?:and|then)\s+)(?P<rest>.+))?$",
    re.IGNORECASE,
)
_CHAT_WORKDIR_FALSE_POSITIVE_TARGETS = {"definition"}
_STRICT_SHELL_SANDBOX_UNAVAILABLE_PREFIX = (
    "Shell sandbox strict mode is enabled, but no usable backend is available:"
)


def _strip_rich_markup(text: Any) -> str:
    """Best-effort strip of Rich console markup (``[red]…[/red]``) for plain
    transcript display. The resume status strings carry colour tags meant for the
    classic Rich console; in the TUI they render literally, so flatten them."""
    raw = str(text or "")
    try:
        from rich.text import Text

        return Text.from_markup(raw).plain
    except Exception:  # noqa: BLE001 - malformed markup → fall back to a regex
        return re.sub(r"\[/?[a-zA-Z][^\]]*\]", "", raw)


def _print_chat_missing_model_guidance(console: Any) -> None:
    console.print("[yellow]No model is configured yet.[/yellow]")
    console.print(
        "Run `alysis setup` for guided setup, or `alysis login` to connect your Alysis Code account."
    )
    console.print("You can also set one directly with `alysis config set model <MODEL>`.")


def _sync_tui_session_state(
    tui_state: Any, session: Any, *, include_exec_mode: bool = False
) -> None:
    if include_exec_mode:
        tui_state.exec_mode = str(getattr(session, "mode", "") or "").strip()
        # A persona model swap changes the live client, not cfg.model — the
        # footer should show what will actually answer.
        live_model = str(getattr(getattr(session, "client", None), "model", "") or "")
        if live_model:
            tui_state.model_name = live_model
    # Empty when the feature is off so the footer renders no persona half.
    tui_state.persona = (
        normalize_persona(getattr(session, "persona", "code"), _session_persona_registry(session))
        if persona_modes_enabled(getattr(session, "cfg", None))
        else ""
    )
    try:
        usage_enabled = globals().get("_chat_usage_hud_enabled")
        if callable(usage_enabled):
            tui_state.usage_hud_enabled = bool(usage_enabled(session))
        else:
            tui_state.usage_hud_enabled = bool(getattr(session, "_usage_hud_enabled", True))
    except Exception:
        pass


def _is_default_shell_sandbox_startup_failure(*, cfg: Any, error: Exception) -> bool:
    if not str(error).startswith(_STRICT_SHELL_SANDBOX_UNAVAILABLE_PREFIX):
        return False
    if env_get("ALYSIS_SHELL_SANDBOX_MODE") is not None:
        return False
    extra_fields = getattr(cfg, "extra_fields", {})
    if not isinstance(extra_fields, dict):
        return True
    shell_cfg = extra_fields.get("shell_sandbox")
    if not isinstance(shell_cfg, dict):
        return True
    return shell_cfg.get("mode") is None


def _cfg_with_warn_shell_sandbox_mode(cfg: Any) -> Any:
    extra_fields = dict(getattr(cfg, "extra_fields", {}) or {})
    shell_cfg = dict(extra_fields.get("shell_sandbox") or {})
    shell_cfg["mode"] = "warn"
    extra_fields["shell_sandbox"] = shell_cfg
    return cfg.model_copy(update={"extra_fields": extra_fields}, deep=True)


def _resolve_forge_entry_root(*, session: Any, fallback_root: Path) -> Path:
    session_root = getattr(session, "root", None)
    if session_root is None:
        return Path(fallback_root).resolve()
    return Path(resolve_session_active_workdir_path(session)).resolve()


def _chat_plan_usage_lines() -> tuple[str, ...]:
    return (
        "[yellow]Usage:[/yellow] /plan <task>   default draft/review/approve flow; can execute after approval",
        "                 /plan mode     secondary persistent readonly planning overlay",
        "                 /plan approve  only inside Plan Mode; executes the stored draft",
        "                 /plan off",
        "                 /plan status",
        "[dim]Compatibility:[/dim] /plan draft <task>, /plan readonly, /plan on",
    )


def _chat_plan_already_on_message(*, plan_mode_escape_supported: bool) -> str:
    if plan_mode_escape_supported:
        return "Plan Mode already on. Press Esc at an empty prompt or use /plan off to leave."
    return "Plan Mode already on. Use /plan off to leave."


def _chat_plan_draft_blocked_by_mode_lines(*, plan_mode_escape_supported: bool) -> tuple[str, ...]:
    lines = [
        "Cannot start /plan while Plan Mode is on.",
        "Use /plan off first, then use /plan <task> for the default draft/review/approve flow.",
    ]
    if plan_mode_escape_supported:
        lines.append("Press Esc at an empty prompt to leave interactively.")
    return tuple(lines)


def _chat_plan_readonly_mode_guidance_lines() -> tuple[str, ...]:
    return (
        "Cannot start /plan in Read-Only mode.",
        "Switch to /mode review, /mode auto, or /mode fullaccess, then use /plan <task> for the default draft/review/approve flow.",
        "Use /plan mode only when you explicitly want persistent readonly planning.",
    )


_PLAN_MODE_EXECUTE_NOW_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|yes|yeah|yep|sure|please)\s+)?"
    r"(?:do it|go ahead|go for it|implement(?: it)?|execute(?: it)?|"
    r"start(?: implementing| coding)?|proceed|run it|apply it|ship it)"
    r"(?:\s+(?:now|then|please))?$",
    re.IGNORECASE,
)
_PLAN_MODE_NUMBERED_STEP_RE = re.compile(r"^\s{0,3}\d+[.)]\s+\S")
_PLAN_MODE_TASK_PREVIEW_CHARS = 96


def _chat_plan_mode_latest_task(plan_mode_state: Any) -> str | None:
    task = getattr(plan_mode_state, "latest_task", None)
    clean = str(task or "").strip()
    return clean or None


def _chat_plan_mode_latest_draft(plan_mode_state: Any) -> str | None:
    draft = getattr(plan_mode_state, "latest_draft", None)
    clean = str(draft or "").strip()
    return clean or None


def _clear_chat_plan_mode_draft_state(plan_mode_state: Any) -> None:
    if hasattr(plan_mode_state, "latest_task"):
        plan_mode_state.latest_task = None
    if hasattr(plan_mode_state, "latest_draft"):
        plan_mode_state.latest_draft = None


def _store_chat_plan_mode_draft_state(
    plan_mode_state: Any,
    *,
    user_message: str,
    draft: str,
) -> bool:
    task = str(user_message or "").strip()
    clean_draft = str(draft or "").strip()
    previous_task = _chat_plan_mode_latest_task(plan_mode_state)
    previous_draft = _chat_plan_mode_latest_draft(plan_mode_state)
    if hasattr(plan_mode_state, "latest_task"):
        plan_mode_state.latest_task = task or None
    if hasattr(plan_mode_state, "latest_draft"):
        plan_mode_state.latest_draft = clean_draft or None
    return previous_task != (task or None) or previous_draft != (clean_draft or None)


def _chat_plan_task_preview(task: str | None) -> str:
    clean = re.sub(r"\s+", " ", str(task or "").strip())
    if len(clean) <= _PLAN_MODE_TASK_PREVIEW_CHARS:
        return clean
    return clean[: _PLAN_MODE_TASK_PREVIEW_CHARS - 3].rstrip() + "..."


def _looks_like_actionable_plan_mode_draft(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    steps = sum(1 for line in clean.splitlines() if _PLAN_MODE_NUMBERED_STEP_RE.match(line.strip()))
    return steps >= 2


def _latest_assistant_text_since(session: Any, *, start_index: int = 0) -> str | None:
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return None
    start = max(int(start_index or 0), 0)
    for entry in reversed(messages[start:]):
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = str(entry.get("content") or "").strip()
        if text:
            return text
    return None


def _plan_mode_no_stored_draft_lines() -> tuple[str, ...]:
    return (
        "No stored actionable Plan Mode draft is available yet.",
        "Send the concrete implementation request as a normal chat message first.",
        "/plan <task> remains the default draft/review/approve path outside Plan Mode.",
        "Once the host captures a numbered draft, use exact /plan approve to execute it or /plan off to leave.",
    )


def _plan_mode_readonly_origin_lines() -> tuple[str, ...]:
    return (
        "This Plan Mode overlay was entered from plain Read-Only mode.",
        "Exact /plan approve cannot execute into a readonly session.",
        "Use /plan off to leave the overlay, then switch to /mode review, /mode auto, or /mode fullaccess and use /plan <task> for execution.",
    )


def _plan_mode_entry_guidance_lines(
    *,
    restore_mode: str,
) -> tuple[str, ...]:
    if restore_mode == "readonly":
        return (
            "Plan Mode is a persistent readonly planning overlay. It does not execute by itself.",
            "If you want readonly planning here, send the concrete implementation task as a normal chat message.",
            "For the default draft/review/approve flow, use /plan off, switch to /mode review, /mode auto, or /mode fullaccess, then use /plan <task>.",
        )
    return (
        "Plan Mode is a persistent readonly planning overlay. It does not execute by itself.",
        "For the default draft/review/approve flow, leave with /plan off and use /plan <task>.",
        "If you want readonly planning here, send the concrete implementation task as a normal chat message.",
        f"When the latest draft looks right, use exact /plan approve to leave Plan Mode, restore {_chat_mode_display(restore_mode)}, and execute it.",
        "Use /plan off to leave without execution.",
    )


def _plan_mode_execute_now_guidance_lines(
    *,
    plan_mode_state: Any,
    plan_mode_escape_supported: bool,
) -> tuple[str, ...]:
    restore_mode = _chat_plan_mode_restore_mode(plan_mode_state) or "readonly"
    latest_draft = _chat_plan_mode_latest_draft(plan_mode_state)
    lines = [
        "Plan Mode is still on and stays read-only.",
        "/plan <task> remains the default draft/review/approve path outside Plan Mode.",
    ]
    if latest_draft is None:
        lines.extend(_plan_mode_no_stored_draft_lines())
    elif restore_mode == "readonly":
        lines.append("A latest actionable draft is already stored for this session.")
        lines.extend(_plan_mode_readonly_origin_lines())
    else:
        lines.append("A latest actionable draft is already stored for this session.")
        lines.append(
            f"Use exact /plan approve to leave Plan Mode, restore {_chat_mode_display(restore_mode)}, and execute that draft."
        )
        lines.append("Use /plan off to leave without execution.")
    if plan_mode_escape_supported:
        lines.append("Press Esc at an empty prompt to leave interactively.")
    return tuple(lines)


def _is_plan_mode_execute_now_follow_up(user_message: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(user_message or "").strip())
    if not normalized:
        return False
    trimmed = normalized.strip(" \t\r\n.,;:!?")
    return bool(_PLAN_MODE_EXECUTE_NOW_RE.fullmatch(trimmed))


def _parse_chat_plan_command(raw_plan_arg: str) -> tuple[str, str]:
    clean = str(raw_plan_arg or "").strip()
    if not clean:
        return ("draft", "")
    lowered = clean.lower()
    if lowered in {"mode", "readonly", "on", "approve", "off", "status"}:
        return (lowered, "")
    if lowered == "draft":
        return ("draft", "")
    if lowered.startswith("draft "):
        return ("draft", clean[6:].strip())
    return ("draft", clean)


def _strip_wrapping_quotes(text: str) -> str:
    stripped = str(text or "").strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1].strip()
    return stripped


def _classify_chat_workdir_target(*, session: Any, raw_path: str) -> tuple[bool, str | None]:
    requested_path = str(raw_path or "").strip()
    if not requested_path:
        return (False, None)
    lowered = requested_path.casefold()
    if lowered in _CHAT_WORKDIR_FALSE_POSITIVE_TARGETS:
        return (False, None)
    pathlike = (
        "/" in requested_path
        or "\\" in requested_path
        or requested_path.startswith(".")
        or Path(requested_path).is_absolute()
    )
    workspace_root = Path(getattr(session, "root", Path("."))).resolve()
    current_workdir = Path(resolve_session_active_workdir_path(session))
    requested_obj = Path(requested_path)
    candidate = (
        requested_obj.resolve()
        if requested_obj.is_absolute()
        else (current_workdir / requested_obj).resolve()
    )
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        if pathlike:
            return (
                True,
                "Requested path escapes the bound workspace_root. Start a new session for another workspace.",
            )
        return (False, None)
    if not candidate.exists():
        if pathlike:
            return (True, f"Directory does not exist: {candidate}")
        return (False, None)
    if not candidate.is_dir():
        if pathlike:
            return (True, f"Path is not a directory: {candidate}")
        return (False, None)
    return (True, None)


def _parse_chat_workdir_navigation_request(
    *,
    input_text: str,
    session: Any,
) -> tuple[str, str] | tuple[str, str, str] | None:
    trimmed = str(input_text or "").strip()
    lowered = trimmed.casefold()
    prefix = next(
        (item for item in _CHAT_WORKDIR_NAVIGATION_PREFIXES if lowered.startswith(item)), None
    )
    if prefix is None:
        return None
    remainder = trimmed[len(prefix) :].strip()
    if not remainder:
        return None
    match = _CHAT_WORKDIR_TARGET_RE.match(remainder)
    if match is None:
        return None
    raw_requested_path = _strip_wrapping_quotes(match.group("path") or "")
    requested_path = (
        raw_requested_path
        if raw_requested_path in {".", ".."}
        else raw_requested_path.rstrip(".,;:!?")
    )
    if not requested_path:
        return None
    should_handle, error_message = _classify_chat_workdir_target(
        session=session,
        raw_path=requested_path,
    )
    if not should_handle:
        return None
    trailing_instruction = str(match.group("rest") or "").strip()
    if error_message:
        return (requested_path, trailing_instruction, error_message)
    return (requested_path, trailing_instruction)


def _forge_plan_command_guidance_lines(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._forge_plan_command_guidance_lines(*args, **kwargs)


def _print_forge_plan_command_guidance(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._print_forge_plan_command_guidance(*args, **kwargs)


def _open_assets_modal(*, session: Any, console: Console, run_paths: Any) -> None:
    cfg = getattr(session, "cfg", None)
    if not isinstance(cfg, AppConfig):
        cfg = load_config()
    try:
        from ..assets_modal import run_assets_modal

        run_assets_modal(cfg=cfg, run_paths=run_paths, console=console)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Assets modal failed:[/red] {exc}")


def _chat_skill_usage_lines(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._chat_skill_usage_lines(*args, **kwargs)


def _artifact_display_ref(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._artifact_display_ref(*args, **kwargs)


def _render_planner_reply(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._render_planner_reply(*args, **kwargs)


def _render_labeled_chat_message(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._render_labeled_chat_message(*args, **kwargs)


def _render_plan_draft(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._render_plan_draft(*args, **kwargs)


def _chat_plan_mode_enabled(plan_mode_state: Any) -> bool:
    return bool(getattr(plan_mode_state, "enabled", False))


def _chat_plan_mode_restore_mode(plan_mode_state: Any) -> str | None:
    restore_mode = getattr(plan_mode_state, "restore_mode", None)
    if restore_mode is None:
        return None
    normalized = str(restore_mode).strip().lower()
    return normalized or None


def _render_chat_plan_mode_status(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._render_chat_plan_mode_status(*args, **kwargs)


def _disable_chat_plan_mode(
    *,
    session: Any,
    console: Console,
    plan_mode_state: Any,
    clear_draft: bool = True,
) -> str | None:
    restore_mode = _chat_plan_mode_restore_mode(plan_mode_state) or "review"
    current_mode = str(getattr(session, "mode", "review")).strip().lower() or "review"
    previous_task = _chat_plan_mode_latest_task(plan_mode_state)
    previous_draft = _chat_plan_mode_latest_draft(plan_mode_state)
    plan_mode_state.enabled = False
    plan_mode_state.restore_mode = None
    if clear_draft:
        _clear_chat_plan_mode_draft_state(plan_mode_state)
    if current_mode != restore_mode:
        try:
            _apply_chat_effective_mode(
                session=session,
                next_mode=restore_mode,
                persist_default_mode=False,
            )
        except Exception as e:  # noqa: BLE001
            plan_mode_state.enabled = True
            plan_mode_state.restore_mode = restore_mode
            if hasattr(plan_mode_state, "latest_task"):
                plan_mode_state.latest_task = previous_task
            if hasattr(plan_mode_state, "latest_draft"):
                plan_mode_state.latest_draft = previous_draft
            console.print(f"[red]Failed to disable Plan Mode:[/red] {e}")
            return None
    console.print(
        f"Plan Mode set for this session: off (restored {_chat_mode_display(restore_mode)})"
    )
    return restore_mode


def _record_plan_mode_draft_from_turn(
    *,
    session: Any,
    console: Console,
    plan_mode_state: Any,
    user_message: str,
    start_index: int,
) -> None:
    latest_reply = _latest_assistant_text_since(session, start_index=start_index)
    if latest_reply is None or not _looks_like_actionable_plan_mode_draft(latest_reply):
        return
    changed = _store_chat_plan_mode_draft_state(
        plan_mode_state,
        user_message=user_message,
        draft=latest_reply,
    )
    if not changed:
        return
    restore_mode = _chat_plan_mode_restore_mode(plan_mode_state) or "readonly"
    task_preview = _chat_plan_task_preview(user_message)
    console.print(f"[dim]Stored latest Plan Mode draft for:[/dim] {task_preview}")
    if restore_mode == "readonly":
        console.print(
            "[dim]This overlay started from Read-Only mode, so exact /plan approve cannot execute it here.[/dim]"
        )
        return
    console.print(
        f"[dim]Use exact /plan approve to leave Plan Mode, restore {_chat_mode_display(restore_mode)}, and execute it.[/dim]"
    )


def _apply_chat_effective_mode(
    *,
    session: Any,
    next_mode: str,
    persist_default_mode: bool,
) -> None:
    _rebuild_session_tools_for_mode(session=session, mode=next_mode)
    session.mode = next_mode
    if persist_default_mode and hasattr(session, "cfg"):
        session.cfg.default_mode = next_mode
    refresh_session_environment_context_message(session)
    surface = getattr(session, "surface", None)
    emit_mode_changed = getattr(surface, "emit_mode_changed", None)
    if callable(emit_mode_changed):
        emit_mode_changed(next_mode)


def _apply_chat_persona(
    *,
    session: Any,
    persona: str,
    source: str = "user",
) -> str:
    """Apply a persona to the live session; returns the effective exec mode.

    The persona is a convention: it selects a default execution mode through
    the clamp rule (lower freely, never raise above the user's chosen mode)
    and everything enforcement-related still flows through
    ``_apply_chat_effective_mode`` -> ``build_tools`` guards. Personas whose
    default keeps the user's mode (code/debug) restore any mode narrowed by a
    previous persona (architect/ask).
    """
    from ...agent.prompt_context import refresh_session_environment_context_message

    definition = get_persona(persona, _session_persona_registry(session))
    current_mode = str(getattr(session, "mode", "review") or "review").strip().lower()
    restore = getattr(session, "persona_restore_mode", None)
    restore_active = restore is not None
    base_mode = str(restore or current_mode).strip().lower()
    base_globs = (
        getattr(session, "persona_restore_write_globs", None)
        if restore_active
        else getattr(session, "allow_write_globs", None)
    )
    target_mode = resolve_persona_exec_mode(definition, base_mode)
    target_persona_globs = (
        list(definition.allow_write_globs) if definition.allow_write_globs else None
    )
    swap = _resolve_persona_model_swap(session, definition)
    prepared_client: Any | None = None
    prepared_client_key: tuple[str, float] | None = None
    if swap is not None:
        swap_role, swap_model, swap_temperature = swap
        try:
            prepared_client, prepared_client_key = _prepare_session_client_for_persona(
                session=session,
                role=swap_role,
                model=swap_model,
                temperature=swap_temperature,
            )
        except Exception as exc:
            store_obj = getattr(session, "store", None)
            append_fail = getattr(store_obj, "append", None)
            if callable(append_fail):
                append_fail(
                    "persona_model_swap_failed",
                    {
                        "persona": definition.name,
                        "model": swap_model,
                        "error": sanitize_error_text_for_output(exc),
                    },
                )
            # A configured persona model is part of the switch contract. Do
            # not silently land the persona on a different client, and do not
            # mutate any persona state after strict-policy rejection.
            raise
    narrows = bool(definition.default_exec_mode) or bool(definition.allow_write_globs)
    session.persona = definition.name
    if narrows:
        session.persona_restore_mode = base_mode
        session.persona_restore_write_globs = list(base_globs) if base_globs is not None else None
    else:
        session.persona_restore_mode = None
        session.persona_restore_write_globs = None
    current_persona_globs = getattr(session, "persona_allow_write_globs", None)
    scope_changed = (current_persona_globs or None) != (target_persona_globs or None)
    # Preserve the user's base scope and give the gate both constraints. It
    # evaluates them conjunctively instead of trying to synthesize glob
    # intersections (which is incomplete for general glob languages).
    session.allow_write_globs = list(base_globs) if base_globs is not None else None
    session.persona_allow_write_globs = target_persona_globs
    if prepared_client is not None and prepared_client_key is not None:
        session.client = prepared_client
        session.persona_client_key = prepared_client_key
    if target_mode != current_mode or scope_changed:
        # The rebuild inside _apply_chat_effective_mode re-reads the session's
        # allow_write_globs, so a scope-only change still needs it.
        _apply_chat_effective_mode(
            session=session,
            next_mode=target_mode,
            persist_default_mode=False,
        )
    else:
        refresh_session_environment_context_message(session)
    surface = getattr(session, "surface", None)
    emit_persona_changed = getattr(surface, "emit_persona_changed", None)
    if callable(emit_persona_changed):
        emit_persona_changed(definition.name, target_mode, source)
    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    if callable(append):
        append(
            "persona_switch_applied",
            {
                "persona": definition.name,
                "effective_mode": target_mode,
                "source": source,
                "model": str(getattr(getattr(session, "client", None), "model", "") or ""),
            },
        )
    return target_mode


def _apply_startup_persona(*, session: Any, console: Any = None) -> None:
    """Apply a non-default ``default_persona`` once at chat startup.

    Session creation records ``session.persona`` from config but deliberately
    leaves the execution mode alone (agent core stays persona-agnostic); the
    chat loop owns applying the persona's default mode, exactly as it owns
    every later transition.
    """
    cfg = getattr(session, "cfg", None)
    if not persona_modes_enabled(cfg):
        return
    for warning in getattr(session, "persona_registry_warnings", ()) or ():
        if console is not None:
            console.print(f"[yellow]Custom persona:[/yellow] {warning}")
    registry = _session_persona_registry(session)
    configured = str(getattr(cfg, "default_persona", "code") or "code").strip().lower()
    persona = normalize_persona(getattr(session, "persona", "code"), registry)
    if (
        console is not None
        and configured != DEFAULT_PERSONA
        and normalize_persona(configured, registry) == DEFAULT_PERSONA
    ):
        console.print(f"[yellow]Unknown persona {configured!r}; starting as code.[/yellow]")
    if persona == "code" or getattr(session, "persona_restore_mode", None) is not None:
        return
    try:
        _apply_chat_persona(session=session, persona=persona, source="config")
    except Exception as exc:  # noqa: BLE001
        if console is not None:
            console.print(f"[red]Failed to apply default persona {persona}:[/red] {exc}")
        return
    if console is not None:
        console.print(f"[dim]Persona: {persona}[/dim]")


def _session_persona_registry(session: Any) -> Any:
    return getattr(session, "persona_registry", None)


def _resolve_persona_model_swap(session: Any, definition: Any) -> tuple[str, str, float] | None:
    """Resolved ``(role, model, temperature)`` when the client must change.

    Resolution is entirely the existing chain (persona_models -> role ->
    resolve_model_for_role precedence). With no role/persona model config the
    chain lands on ``cfg.model`` for every persona, so default installs never
    swap — sticky persona models activate only when the user configured them.
    """
    cfg = getattr(session, "cfg", None)
    client_obj = getattr(session, "client", None)
    if client_obj is None:
        # No live client (bare test/session shims): nothing to swap.
        return None
    from ...config import resolve_role_temperature
    from ...model_router import resolve_model_for_role
    from ...personas import resolve_persona_model_role

    role = resolve_persona_model_role(cfg, definition.name, _session_persona_registry(session))
    target = str(resolve_model_for_role(cfg=cfg, role=role, plan=None) or "").strip()
    temperature = float(resolve_role_temperature(cfg, role=role))
    if not target:
        return None
    target_key = (target, temperature)
    current_key = getattr(session, "persona_client_key", None)
    if current_key is None:
        current_model = str(getattr(client_obj, "model", "") or "").strip()
        current_temperature = getattr(client_obj, "temperature", None)
        try:
            current_key = (current_model, float(current_temperature))
        except (TypeError, ValueError):
            # Bare test/session shims may not expose temperature. Preserve the
            # historical no-op when the model itself is unchanged; real
            # sessions always record persona_client_key.
            if target == current_model:
                return None
    if target_key == current_key:
        return None
    return role, target, temperature


def _prepare_session_client_for_persona(
    *, session: Any, role: str, model: str, temperature: float
) -> tuple[Any, tuple[str, float]]:
    """Validate and construct a persona client without changing live state.

    Strict metadata policy applies to every model that can become active, not
    only the model present at session creation. Clients are cached by the
    resolved inputs that affect their behavior, so roles sharing a model but
    using different temperatures cannot contaminate one another.
    """
    from ...agent.session import _make_session_llm_client
    from ...config import (
        resolve_llm_enable_thinking,
        resolve_llm_reasoning_effort,
        resolve_llm_timeout_s,
        resolve_prompt_cache_key,
        resolve_prompt_cache_retention,
    )
    from ...model_metadata_policy import (
        ActiveModelRef,
        evaluate_active_model_metadata_policy,
    )
    from ...model_registry import ModelRegistry

    cfg = getattr(session, "cfg", None)
    registry = getattr(session, "model_registry", None)
    if registry is None:
        registry = ModelRegistry(
            cfg=cfg,
            api_key=str(getattr(session, "api_key", "") or ""),
        )
    policy_result = evaluate_active_model_metadata_policy(
        cfg=cfg,
        registry=registry,
        active_models=[ActiveModelRef(role=role, model_name=model)],
    )
    surface = getattr(session, "surface", None)
    emit_warning = getattr(surface, "on_warning", None)
    for warning_message in policy_result.warning_messages:
        if callable(emit_warning):
            emit_warning(warning_message)

    cache = getattr(session, "persona_client_cache", None)
    if cache is None:
        cache = {}
        session.persona_client_cache = cache
    current_key = getattr(session, "persona_client_key", None)
    current_client = getattr(session, "client", None)
    if current_key is not None and current_client is not None:
        cache.setdefault(current_key, current_client)
    target_key = (model, temperature)
    client = cache.get(target_key)
    if client is None:
        client = _make_session_llm_client(
            cfg=cfg,
            api_key=str(getattr(session, "api_key", "") or ""),
            model=model,
            timeout_s=resolve_llm_timeout_s(cfg),
            temperature=temperature,
            prompt_cache_key=getattr(
                session,
                "prompt_cache_stream_key",
                resolve_prompt_cache_key(cfg),
            ),
            prompt_cache_retention=resolve_prompt_cache_retention(cfg),
            prompt_cache_namespace=None,
            enable_thinking=resolve_llm_enable_thinking(cfg),
            reasoning_effort=resolve_llm_reasoning_effort(cfg),
            session_id=None,
        )
        cache[target_key] = client
    return client, target_key


def _load_resumed_persona(session: Any) -> str | None:
    """Last applied persona from the resumed session's log, or None.

    Resume restores the *base* execution mode from ``session_start`` and
    leaves the persona at the config default; the ``persona_switch_applied``
    events (user switches, Tab cycles, approved switch_mode proposals, and
    the startup application) carry the rest. Last-wins scan, same pattern as
    the chat-resume runtime-settings loaders.
    """
    from ...session_store import read_session_events
    from ..commands.chat_resume_helpers import _resolve_session_log_path

    if not persona_modes_enabled(getattr(session, "cfg", None)):
        return None
    try:
        log_path = _resolve_session_log_path(session)
    except Exception:  # noqa: BLE001 - resume must not fail on persona lookup
        return None
    if log_path is None:
        return None
    persona: str | None = None
    try:
        for event in read_session_events(log_path):
            if str(event.get("type") or "") != "persona_switch_applied":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict):
                candidate = str(payload.get("persona") or "").strip().lower()
                if candidate:
                    persona = candidate
    except Exception:  # noqa: BLE001
        return None
    return persona


def _reapply_resumed_persona(*, session: Any, console: Any = None) -> None:
    """Re-apply the resumed session's last persona after the state swap.

    ``session_start`` restores the user's base mode; re-applying the persona
    on top reproduces the narrowed mode, write scope, restore point, and
    model role exactly as the clamp would have left them. The no-op Code
    persona needs no application — the restored base state already is the
    correct state.
    """
    persona = _load_resumed_persona(session)
    if not persona:
        return
    normalized = normalize_persona(persona, _session_persona_registry(session))
    if (
        normalized == DEFAULT_PERSONA
        and normalize_persona(getattr(session, "persona", DEFAULT_PERSONA)) == DEFAULT_PERSONA
    ):
        return
    try:
        _apply_chat_persona(session=session, persona=normalized, source="resume")
    except Exception as exc:  # noqa: BLE001
        if console is not None:
            console.print(f"[yellow]Could not restore persona {normalized}:[/yellow] {exc}")
        return
    if console is not None and normalized != DEFAULT_PERSONA:
        console.print(f"[dim]Persona restored: {normalized}[/dim]")


def _clear_persona_restore(session: Any) -> None:
    """An explicit user /mode <exec> redefines the base: restore any
    persona-narrowed write scope first, then drop the restore point."""
    if getattr(session, "persona_restore_mode", None) is not None:
        session.allow_write_globs = getattr(session, "persona_restore_write_globs", None)
    session.persona_allow_write_globs = None
    session.persona_restore_mode = None
    session.persona_restore_write_globs = None


def _apply_pending_persona_switch(*, session: Any, console: Any = None) -> None:
    """Apply a user-approved switch_mode proposal after the turn ends.

    The tool only parks the approved target in the session's
    ``PersonaSwitchState``; applying it here keeps the tool surface stable for
    the whole turn and reuses the exact primitive user switches go through.
    """
    state = getattr(session, "persona_switch_state", None)
    pending = getattr(state, "pending", None)
    if not pending:
        return
    state.pending = None
    persona, _reason = pending
    try:
        _apply_chat_persona(session=session, persona=persona, source="model")
    except Exception as exc:  # noqa: BLE001
        if console is not None:
            console.print(f"[red]Failed to apply approved persona switch:[/red] {exc}")
        return
    if console is not None:
        console.print(f"[dim]Persona → {persona}[/dim]")


class _ConfigReloadRequiresRestart(RuntimeError):
    """Raised when a saved config cannot be applied to the current session topology."""


_ROUTE_LOCAL_CLIENT_OPTIONAL_FLAGS = (
    "_reasoning_summary_supported",
    "_thinking_display_supported",
    "_thought_summaries_supported",
    "_thinking_summaries_supported",
    "_input_token_count_available",
    "_cached_content_create_disabled_reason",
)
_ROUTE_LOCAL_CLIENT_FALSE_FLAGS = ("_temperature_omit_after_rejection",)
_ROUTE_LOCAL_CLIENT_ZERO_FIELDS = ("_cached_content_create_transient_failures",)
_ROUTE_LOCAL_CLIENT_CONTAINERS = (
    ("_cached_content_by_signature", None),
    ("_disabled_prompt_cache_fields", "_disabled_prompt_cache_fields_lock"),
    ("_temperature_compat_modes", "_temperature_compat_lock"),
    ("_tool_choice_compat_disabled", "_tool_choice_compat_lock"),
    ("_tool_calling_compat_disabled", "_tool_calling_compat_lock"),
)


def _reset_route_local_client_capabilities(client: Any, *, route_changed: bool) -> None:
    if not route_changed:
        return
    for field_name in _ROUTE_LOCAL_CLIENT_OPTIONAL_FLAGS:
        if hasattr(client, field_name):
            setattr(client, field_name, None)
    for field_name in _ROUTE_LOCAL_CLIENT_FALSE_FLAGS:
        if hasattr(client, field_name):
            setattr(client, field_name, False)
    for field_name in _ROUTE_LOCAL_CLIENT_ZERO_FIELDS:
        if hasattr(client, field_name):
            setattr(client, field_name, 0)
    for field_name, lock_field_name in _ROUTE_LOCAL_CLIENT_CONTAINERS:
        container = getattr(client, field_name, None)
        clear = getattr(container, "clear", None)
        if not callable(clear):
            continue
        lock = getattr(client, lock_field_name, None) if lock_field_name else None
        if lock is None:
            clear()
            continue
        with lock:
            clear()


_RELOAD_SESSION_FIELDS = (
    "cfg",
    "routing_mode",
    "max_steps",
    "api_key",
    "api_key_source",
    "model_registry",
)
_RELOAD_CLIENT_FIELDS = (
    "base_url",
    "provider_auth",
    "api_key",
    "model",
    "timeout_s",
    "temperature",
    "prompt_cache_key",
    "prompt_cache_retention",
    "prompt_cache_request_field_values",
    "prompt_cache_policy_metadata",
    "prompt_cache_control_enabled",
    "prompt_cache_control_ttl",
    "explicit_cached_content_enabled",
    "cached_content_ttl",
    "cached_content_min_tokens",
    "cached_content_ttl_seconds",
    "cached_content_refresh_margin_seconds",
    "_cached_content_by_signature",
    "_cached_content_create_disabled_reason",
    "_cached_content_create_transient_failures",
    "enable_thinking",
    "reasoning_effort",
    "extra_headers",
    "provider_key",
    "reasoning_trace_adapter",
    "route_identity",
    "reasoning_trace_capability",
    "provider_concurrency_caps",
    "provider_retry_settings",
    *_ROUTE_LOCAL_CLIENT_OPTIONAL_FLAGS,
    *_ROUTE_LOCAL_CLIENT_FALSE_FLAGS,
    *_ROUTE_LOCAL_CLIENT_ZERO_FIELDS,
    *tuple(field_name for field_name, _lock_name in _ROUTE_LOCAL_CLIENT_CONTAINERS),
)
_RELOAD_MISSING = object()


def _reload_snapshot_value(value: Any) -> Any:
    if isinstance(value, (dict, list, set, tuple)):
        return copy.deepcopy(value)
    return value


def _reload_clients(session: Any) -> list[Any]:
    candidates = [
        getattr(session, "client", None),
        getattr(
            getattr(session, "conversation_compactor", None),
            "compactor_client",
            None,
        ),
    ]
    clients: list[Any] = []
    seen: set[int] = set()
    for client in candidates:
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        clients.append(client)
    return clients


def _snapshot_config_reload_state(
    session: Any,
) -> tuple[dict[str, Any], list[tuple[Any, dict[str, Any]]]]:
    session_snapshot = {
        field_name: (
            _reload_snapshot_value(getattr(session, field_name))
            if hasattr(session, field_name)
            else _RELOAD_MISSING
        )
        for field_name in _RELOAD_SESSION_FIELDS
    }
    client_snapshots = [
        (
            client,
            {
                field_name: (
                    _reload_snapshot_value(getattr(client, field_name))
                    if hasattr(client, field_name)
                    else _RELOAD_MISSING
                )
                for field_name in _RELOAD_CLIENT_FIELDS
            },
        )
        for client in _reload_clients(session)
    ]
    return session_snapshot, client_snapshots


def _restore_config_reload_state(
    session: Any,
    snapshot: tuple[dict[str, Any], list[tuple[Any, dict[str, Any]]]],
) -> None:
    session_snapshot, client_snapshots = snapshot
    for field_name, value in session_snapshot.items():
        if value is _RELOAD_MISSING:
            if hasattr(session, field_name):
                delattr(session, field_name)
            continue
        setattr(session, field_name, _reload_snapshot_value(value))
    for client, fields in client_snapshots:
        for field_name, value in fields.items():
            if value is _RELOAD_MISSING:
                if hasattr(client, field_name):
                    delattr(client, field_name)
                continue
            setattr(client, field_name, _reload_snapshot_value(value))


def _apply_config_menu_changes_to_session(*, session: Any, cfg: AppConfig) -> None:
    snapshot = _snapshot_config_reload_state(session)
    try:
        _apply_config_menu_changes_to_session_mutating(session=session, cfg=cfg)
    except Exception:
        _restore_config_reload_state(session, snapshot)
        raise


def _apply_config_menu_changes_to_session_mutating(*, session: Any, cfg: AppConfig) -> None:
    from ...config import (
        ConfigError,
        clone_cfg,
        resolve_api_key,
        resolve_llm_enable_thinking,
        resolve_llm_reasoning_effort,
        resolve_llm_timeout_s,
        resolve_prompt_cache_key,
        resolve_prompt_cache_retention,
        resolve_role_temperature,
    )
    from ...llm.cache_capabilities import resolve_effective_cache_capability
    from ...llm.cache_policy import build_prompt_cache_namespace, resolve_prompt_cache_policy
    from ...llm.metadata import (
        build_provider_route_identity,
        canonicalize_extra_headers,
        credential_scope_fingerprint,
    )
    from ...llm.protocols import (
        ANTHROPIC_MESSAGES_PROTOCOL,
        GEMINI_INTERACTIONS_PROTOCOL,
        OPENAI_COMPAT_PROTOCOL,
        get_provider_protocol_capabilities,
        resolve_reasoning_trace_capability,
    )
    from ...llm.provider_limits import resolve_provider_retry_settings
    from ...model_registry import ModelRegistry, resolve_model_provider_key
    from ...model_router import (
        ROLE_CODING,
        ROLE_COMPACTOR,
        resolve_model_for_role,
    )
    from ...profile_presets import find_preset_for_profile
    from ...profiles import get_active_profile, resolve_effective_base_url
    from ...provider_auth import create_provider_auth

    session.cfg = clone_cfg(cfg)
    # Deprecated no-op field: kept in step with config for observability only.
    session.routing_mode = str(getattr(cfg, "routing_mode", "auto") or "auto")
    fixed_step_override = getattr(session, "chat_turn_fixed_override", None)
    session.max_steps = int(
        fixed_step_override if fixed_step_override is not None else session.cfg.max_steps
    )
    active_profile = get_active_profile(session.cfg)
    effective_base_url = resolve_effective_base_url(cfg=session.cfg, profile=active_profile)
    protocol = str(active_profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    resolved_key = resolve_api_key(session.cfg)
    session.api_key = str(resolved_key.key or "")
    session.api_key_source = (
        f"provider-auth:{active_profile.auth_provider}"
        if active_profile.auth_provider
        else resolved_key.source
    )
    provider_auth = (
        create_provider_auth(active_profile.auth_provider) if active_profile.auth_provider else None
    )
    route_session_scope = credential_scope_fingerprint(
        getattr(getattr(session, "store", None), "session_id", None)
    )

    timeout_s = resolve_llm_timeout_s(session.cfg)
    enable_thinking = resolve_llm_enable_thinking(session.cfg)
    reasoning_effort = resolve_llm_reasoning_effort(session.cfg)
    prompt_cache_key = getattr(
        session,
        "prompt_cache_stream_key",
        resolve_prompt_cache_key(session.cfg),
    )
    prompt_cache_retention = resolve_prompt_cache_retention(session.cfg)
    coding_temperature = resolve_role_temperature(session.cfg, role=ROLE_CODING)
    provider_retry_settings = resolve_provider_retry_settings(session.cfg)
    model_registry = ModelRegistry(cfg=session.cfg, api_key=session.api_key)
    session.model_registry = model_registry

    def _apply_client_config(
        client: Any,
        *,
        model: str,
        role: str,
        temperature: float | None = None,
        disable_reasoning: bool = False,
    ) -> None:
        existing_route_identity = getattr(client, "route_identity", None)
        client.base_url = effective_base_url
        if hasattr(client, "provider_auth"):
            client.provider_auth = provider_auth
        client.api_key = session.api_key
        client.model = model
        client.timeout_s = timeout_s
        if temperature is not None:
            client.temperature = temperature
        provider_key = resolve_model_provider_key(
            cfg=session.cfg,
            model_name=model,
            base_url=effective_base_url,
            profile_name=active_profile.name,
        )
        capabilities = get_provider_protocol_capabilities(
            provider_key=provider_key,
            protocol=protocol,
        )
        preset = find_preset_for_profile(active_profile)
        cache_capability = resolve_effective_cache_capability(
            provider_key=provider_key,
            protocol=protocol,
            model=model,
            base_url=effective_base_url,
            transport_capabilities=capabilities,
            preset_cache_capability=(preset.cache_capability if preset is not None else None),
            profile_cache_capability=active_profile.cache_capability,
        )
        cache_policy = resolve_prompt_cache_policy(
            cfg=session.cfg,
            capabilities=capabilities,
            provider_key=provider_key,
            protocol=protocol,
            model=model,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
            prompt_cache_namespace=build_prompt_cache_namespace(
                workspace_root=getattr(session, "root", None),
                role=role,
                profile_name=active_profile.name,
            ),
            cache_capability=cache_capability,
        )
        credential_scope = credential_scope_fingerprint(session.api_key)
        if provider_auth is not None:
            auth_scope = getattr(provider_auth, "route_credential_scope", None)
            if callable(auth_scope):
                credential_scope = str(auth_scope() or "").strip() or credential_scope
        protocol_revision = ""
        if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
            from ...llm import anthropic_messages

            protocol_revision = anthropic_messages.ANTHROPIC_MESSAGES_ROUTE_REVISION
        elif protocol == GEMINI_INTERACTIONS_PROTOCOL:
            from ...llm import gemini_interactions

            protocol_revision = gemini_interactions.GEMINI_INTERACTIONS_ROUTE_REVISION
        next_route_identity = build_provider_route_identity(
            protocol=protocol,
            base_url=effective_base_url,
            provider_key=provider_key,
            model=model,
            profile_name=active_profile.name,
            auth_provider=active_profile.auth_provider,
            credential_scope=credential_scope,
            routing_headers=active_profile.extra_headers,
            routing_fields=dict(cache_policy.request_field_values),
            reasoning_state_adapter=active_profile.reasoning_trace_adapter,
            protocol_revision=protocol_revision,
            session_scope=(
                route_session_scope
                or str(getattr(existing_route_identity, "session_scope", "") or "")
            ),
        )
        route_changed = (
            str(getattr(existing_route_identity, "fingerprint", "") or "")
            != next_route_identity.fingerprint
        )
        client.route_identity = next_route_identity
        _reset_route_local_client_capabilities(client, route_changed=route_changed)
        client.prompt_cache_key = cache_policy.prompt_cache_key
        client.prompt_cache_retention = cache_policy.prompt_cache_retention
        if hasattr(client, "prompt_cache_request_field_values"):
            client.prompt_cache_request_field_values = dict(cache_policy.request_field_values)
        if hasattr(client, "prompt_cache_policy_metadata"):
            client.prompt_cache_policy_metadata = cache_policy.telemetry_metadata()
        if hasattr(client, "prompt_cache_control_enabled"):
            client.prompt_cache_control_enabled = cache_policy.anthropic_cache_control_enabled
        if hasattr(client, "prompt_cache_control_ttl"):
            client.prompt_cache_control_ttl = cache_policy.anthropic_cache_control_ttl
        if hasattr(client, "explicit_cached_content_enabled") or hasattr(
            client,
            "cached_content_ttl",
        ):
            previous_enabled = bool(getattr(client, "explicit_cached_content_enabled", False))
            previous_ttl = str(getattr(client, "cached_content_ttl", "") or "")
            previous_min_tokens = getattr(client, "cached_content_min_tokens", None)
            next_enabled = bool(cache_policy.gemini_explicit_cached_content_enabled)
            next_ttl = str(cache_policy.gemini_cached_content_ttl or "3600s").strip() or "3600s"
            next_min_tokens = (
                int(cache_policy.min_cacheable_tokens)
                if cache_policy.min_cacheable_tokens is not None
                else previous_min_tokens
            )
            apply_cache_settings = getattr(client, "apply_cache_settings", None)
            if callable(apply_cache_settings):
                apply_cache_settings(
                    enabled=next_enabled,
                    ttl=next_ttl,
                    min_tokens=(
                        max(0, int(next_min_tokens)) if next_min_tokens is not None else None
                    ),
                )
            else:
                if hasattr(client, "explicit_cached_content_enabled"):
                    client.explicit_cached_content_enabled = next_enabled
                if hasattr(client, "cached_content_ttl"):
                    client.cached_content_ttl = next_ttl
                if hasattr(client, "cached_content_min_tokens") and next_min_tokens is not None:
                    client.cached_content_min_tokens = max(0, int(next_min_tokens))
                if (
                    previous_enabled != next_enabled
                    or previous_ttl != next_ttl
                    or previous_min_tokens != next_min_tokens
                ):
                    cached_content_by_signature = getattr(
                        client,
                        "_cached_content_by_signature",
                        None,
                    )
                    clear_cached_content = getattr(cached_content_by_signature, "clear", None)
                    if callable(clear_cached_content):
                        clear_cached_content()
        # The router/classification client is built reasoning-off in session.py so
        # latency-sensitive routing/non-repo turns stay fast on slow reasoning
        # models (e.g. Xiaomi MiMo via the trial proxy). Re-applying the
        # session-wide reasoning settings here would silently re-enable that
        # chain-of-thought and reintroduce the timeout->clarification-fallback bug,
        # so honor disable_reasoning for that client.
        if disable_reasoning:
            client.enable_thinking = False
            client.reasoning_effort = None
        else:
            client.enable_thinking = enable_thinking
            client.reasoning_effort = reasoning_effort
        if hasattr(client, "extra_headers"):
            client.extra_headers = canonicalize_extra_headers(active_profile.extra_headers)
        if hasattr(client, "provider_key"):
            client.provider_key = provider_key
        if hasattr(client, "reasoning_trace_adapter"):
            client.reasoning_trace_adapter = active_profile.reasoning_trace_adapter
        model_meta = model_registry.get(model, include_provider_auth=False)
        model_supports_reasoning = model_meta.supports_reasoning
        model_capability_source = model_meta.field_sources.get("supports_reasoning")
        if active_profile.auth_provider and active_profile.reasoning_effort is not None:
            model_supports_reasoning = True
            model_capability_source = "profile:reasoning_effort"
        client.reasoning_trace_capability = resolve_reasoning_trace_capability(
            provider_key=provider_key,
            protocol=protocol,
            adapter_override=active_profile.reasoning_trace_adapter,
            model_supports_reasoning=model_supports_reasoning,
            model_capability_source=model_capability_source,
        )
        if hasattr(client, "provider_concurrency_caps"):
            client.provider_concurrency_caps = dict(session.cfg.provider_concurrency_caps)
        if hasattr(client, "provider_retry_settings"):
            client.provider_retry_settings = provider_retry_settings

    client = getattr(session, "client", None)
    if client is not None:
        _apply_client_config(
            client,
            model=str(session.cfg.model or ""),
            role=ROLE_CODING,
            temperature=coding_temperature,
        )

    compactor = getattr(session, "conversation_compactor", None)
    compactor_client = getattr(compactor, "compactor_client", None)
    if compactor_client is not None:
        compactor_model = str(session.cfg.model or "")
        try:
            compactor_model = resolve_model_for_role(
                cfg=session.cfg,
                role=ROLE_COMPACTOR,
                plan=None,
            )
        except ConfigError:
            compactor_model = str(session.cfg.model or "")
        _apply_client_config(
            compactor_client,
            model=compactor_model,
            role=ROLE_COMPACTOR,
            temperature=resolve_role_temperature(session.cfg, role=ROLE_COMPACTOR),
        )

    _rebuild_session_tools_for_mode(
        session=session,
        mode=str(getattr(session, "mode", "review") or "review"),
    )
    refresh_session_environment_context_message(session)
    _refresh_chat_hud_context_cache(session)


def _resolve_interactive_plan_mode_request(
    *,
    session: Any,
    console: Console,
    plan_mode_state: Any,
    user_message: str,
    plan_mode_escape_supported: bool = False,
) -> str | _ChatExecutionRequest:
    _ = session
    if _is_plan_mode_execute_now_follow_up(user_message):
        for line in _plan_mode_execute_now_guidance_lines(
            plan_mode_state=plan_mode_state, plan_mode_escape_supported=plan_mode_escape_supported
        ):
            console.print(line)
        return "handled"
    from ...interactive_plan_mode import INTERACTIVE_PLAN_MODE_SYSTEM_PROMPT

    return _ChatExecutionRequest(
        instruction=user_message,
        routing_mode_override="code_only",
        ephemeral_system_messages=(INTERACTIVE_PLAN_MODE_SYSTEM_PROMPT,),
        plan_mode_capture_task=user_message,
    )


def _clone_chat_startup_messages(session: Any) -> list[dict[str, Any]]:
    startup_messages_obj = getattr(session, "startup_messages", None)
    if isinstance(startup_messages_obj, list) and startup_messages_obj:
        cloned = [dict(message) for message in startup_messages_obj if isinstance(message, dict)]
        if cloned:
            return cloned

    messages_obj = getattr(session, "messages", None)
    pinned_prefix_len = max(0, int(getattr(session, "pinned_prefix_len", 0) or 0))
    if not isinstance(messages_obj, list) or pinned_prefix_len <= 0:
        return []
    return [
        dict(message) for message in messages_obj[:pinned_prefix_len] if isinstance(message, dict)
    ]


def _clear_chat_conversation(*, session: Any, pending_images: list[str]) -> None:
    startup_messages = _clone_chat_startup_messages(session)
    if not startup_messages:
        raise RuntimeError("session startup messages unavailable")

    store = getattr(session, "store", None)
    if store is None or not hasattr(store, "append"):
        raise RuntimeError("session store unavailable")
    store.append("conversation_cleared", {"trigger": "user_command"})

    session.messages = startup_messages
    invalidate_request_context = getattr(session, "invalidate_request_context", None)
    if callable(invalidate_request_context):
        invalidate_request_context(reason="conversation_cleared")
    elif hasattr(session, "request_context_measurement"):
        session.request_context_measurement = None
    refresh_session_workspace_binding_context_message(session)
    refresh_session_environment_context_message(session)

    compactor = getattr(session, "conversation_compactor", None)
    if compactor is not None and hasattr(compactor, "state"):
        pinned_prefix_len = max(0, int(getattr(compactor.state, "pinned_prefix_len", 0) or 0))
        compactor.state = CompactionState(
            summary={},
            history_chunk_index=0,
            memory_message_index=None,
            pinned_prefix_len=pinned_prefix_len,
            pins=[],
            pins_message_index=None,
        )

    pending_images.clear()
    _refresh_chat_hud_context_cache(session)


def _handle_chat_command(*args: Any, **kwargs: Any) -> Any:
    from . import commands as _commands

    _commands._sync_command_globals(globals())
    return _commands._handle_chat_command(*args, **kwargs)


def _handle_forge_chat_command(*args: Any, **kwargs: Any) -> Any:
    from . import commands as _commands

    _commands._sync_command_globals(globals())
    return _commands._handle_forge_chat_command(*args, **kwargs)


def _finalize_deferred_forge_execution(
    *,
    surface: Any,
    token: Any,
    paths: Any,
    forge_state: Any,
    captured: str,
    completed: bool,
) -> None:
    """Close out a TUI ``/execute plan`` worker run honestly.

    A cancelled token does NOT mean the run died: ``run_swarm`` never receives
    the token, so a soft-interrupt at any point still lets the swarm run to its
    natural end and the handler return normally (``completed``). The old logic
    keyed everything on the token alone, which silently discarded a finished
    run's entire completion output and froze the view as "Interrupted." — the
    user never learned the run had actually completed.
    """
    if surface is None:
        return
    cancelled = bool(getattr(token, "is_cancelled", False))
    run_attempted = bool(getattr(forge_state, "swarm_run_attempted", False))
    out = str(captured or "").rstrip("\n")
    if out and (completed or not cancelled):
        try:
            surface.append_system(out)
        except Exception:
            pass
    try:
        if cancelled and not completed:
            interrupt_forge = getattr(surface, "interrupt_forge", None)
            if callable(interrupt_forge):
                interrupt_forge()
        elif completed and not run_attempted:
            # The handler rejected the execute before run_swarm (empty plan, no
            # execution-ready tasks, guard failure): say so instead of freezing
            # a "Finished · 0 done · N remaining" table for a run that never
            # started (the rejection reason is in the note above).
            surface.end_forge(
                summary="Execution did not start — see the note above.",
                paths=paths,
            )
        elif cancelled:
            # The Esc-time UI handler already froze the view as "Interrupted."
            # and cleared the surface's run paths; re-arm them so the frozen
            # table reflects the statuses the finished swarm actually wrote.
            surface.end_forge(paths=paths)
        else:
            surface.end_forge()
    except Exception:
        pass


def _planner_workspace_context_for_session(*, session: Any) -> dict[str, Any] | None:
    cached = getattr(session, "planner_workspace_context", None)
    if isinstance(cached, dict):
        return cached
    root_obj = getattr(session, "root", None)
    if root_obj is None:
        return None
    try:
        workspace_root = Path(root_obj)
        workspace_context = resolve_workspace_context(workspace_root)
        scan = scan_workspace(context=workspace_context)
        payload = scan.to_dict()
    except Exception:  # noqa: BLE001
        return None
    try:
        session.planner_workspace_context = payload
    except Exception:  # noqa: BLE001
        pass
    return payload


def _should_defer_forge_planner_submission(*, forge_state: Any, text: str) -> bool:
    """Return whether a Forge submission will perform a planner model call.

    Plain text in Forge is planner input only while the planner assistant is
    enabled. Slash/colon commands remain on the synchronous command path because
    they are local state operations (except explicitly deferred commands such as
    ``/execute plan``).
    """
    trimmed = str(text or "").strip()
    return bool(
        trimmed
        and trimmed[:1] not in "/:"
        and getattr(forge_state, "ui_mode", None) == "forge"
        and getattr(forge_state, "assistant_enabled", False)
    )


def _finish_chat_surface_activity(*, session: Any) -> None:
    surface = getattr(session, "surface", None)
    handler = getattr(surface, "on_assistant_message_done", None)
    if not callable(handler):
        return
    try:
        handler("")
    except Exception:  # noqa: BLE001
        return


def _run_plan_mode_approval_loop(
    *,
    session: Any,
    console: Console,
    user_message: str,
    max_iterations: int | None = None,
    action_prompt: Any | None = None,
) -> str | None:
    limit = max_iterations if max_iterations is not None else MAX_PLAN_ITERATIONS
    if limit <= 0:
        return None

    try:
        from ...llm.types import LLMError as _PlanLLMError
    except Exception:  # noqa: BLE001
        _PlanLLMError = RuntimeError

    previous_plan: str | None = None
    feedback: str | None = None
    for _iteration in range(limit):
        console.print("")
        _render_labeled_chat_message(console=console, label="Request", message=user_message)
        if feedback:
            _render_labeled_chat_message(
                console=console,
                label="Revision feedback",
                message=feedback,
            )
        console.print("")

        if previous_plan and feedback:
            _emit_plan_mode_trace(
                session=session,
                message="Revising draft plan with your feedback.",
            )
        else:
            _emit_plan_mode_trace(
                session=session,
                message="Drafting execution plan for your request.",
            )
        _emit_plan_mode_trace(
            session=session,
            message="Collecting relevant conversation context for planning.",
            full_only=True,
        )
        _emit_plan_mode_trace(
            session=session,
            message="Tools stay disabled while planning this draft.",
            full_only=True,
        )

        session_stream = bool(getattr(session, "stream", False))
        on_text_delta = (
            _make_plan_mode_delta_trace_callback(session=session) if session_stream else None
        )
        stream_plan_draft = session_stream and on_text_delta is not None
        details: dict[str, Any] = {}

        try:
            draft = generate_plan_draft(
                client=getattr(session, "client", None),
                session_messages=list(getattr(session, "messages", []) or []),
                user_message=user_message,
                previous_plan=previous_plan,
                feedback=feedback,
                workspace_context=_planner_workspace_context_for_session(session=session),
                stream=stream_plan_draft,
                on_text_delta=on_text_delta if stream_plan_draft else None,
                details=details,
            )
        except KeyboardInterrupt:
            _finish_chat_surface_activity(session=session)
            console.print("Plan drafting interrupted. Back to chat.")
            return None
        except (RuntimeError, _PlanLLMError) as e:
            if not stream_plan_draft:
                _finish_chat_surface_activity(session=session)
                console.print(f"[red]{e}[/red]")
                return None

            _emit_plan_mode_trace(
                session=session,
                message="Planner stream failed; retrying once without streaming.",
                full_only=True,
            )
            details = {}
            try:
                draft = generate_plan_draft(
                    client=getattr(session, "client", None),
                    session_messages=list(getattr(session, "messages", []) or []),
                    user_message=user_message,
                    previous_plan=previous_plan,
                    feedback=feedback,
                    workspace_context=_planner_workspace_context_for_session(session=session),
                    stream=False,
                    on_text_delta=None,
                    details=details,
                )
            except KeyboardInterrupt:
                _finish_chat_surface_activity(session=session)
                console.print("Plan drafting interrupted. Back to chat.")
                return None
            except (RuntimeError, _PlanLLMError) as retry_error:
                _finish_chat_surface_activity(session=session)
                console.print(f"[red]{retry_error}[/red]")
                return None
        _emit_plan_mode_trace(session=session, message="Plan draft ready for review.")

        request_messages = details.get("request_messages")
        response = details.get("response")
        if isinstance(request_messages, list) and response is not None:
            record_plan_usage(
                session=session,
                request_messages=request_messages,
                response=response,
            )
            _refresh_chat_hud_context_cache(session)

        _finish_chat_surface_activity(session=session)
        _render_plan_draft(console=console, draft=draft)
        console.print("")

        if action_prompt is not None:
            action = action_prompt(console=console, user_message=user_message, draft=draft)
        else:
            action = _prompt_plan_mode_action(console=console)
        if action is None:
            return None
        if action == "approve":
            return instruction_with_approved_plan(user_message=user_message, approved_plan=draft)
        if action == "propose":
            feedback_input = _prompt_plan_mode_feedback(console=console)
            if feedback_input is None:
                return None
            if not feedback_input.strip():
                console.print("[yellow]Feedback cannot be empty.[/yellow]")
                continue
            previous_plan = draft
            feedback = feedback_input.strip()
            _emit_plan_mode_trace(
                session=session,
                message="Captured feedback and preparing a revised draft.",
                full_only=True,
            )
            console.print("[bold]Regenerating plan with your feedback...[/bold]")
            continue

        console.print("Discarded plan. What do you want to build next?")
        return None
    console.print("[yellow]Plan iteration limit reached. Returning to prompt.[/yellow]")
    return None


def _print_chat_context(*args: Any, **kwargs: Any) -> Any:
    from . import rendering as _rendering

    _rendering._sync_rendering_globals(globals())
    return _rendering._print_chat_context(*args, **kwargs)


def chat(
    path: Path = typer.Option(Path("."), "--path", help="Working directory/root."),
    create_path: bool = typer.Option(
        False,
        "--create-path",
        help="Create --path if it does not exist before binding the workspace.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces in non-interactive startup flows.",
    ),
    image: list[Path] | None = typer.Option(
        None,
        "--image",
        help="Queue image path(s) for the next message. Repeat --image for multiple files.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    persona: str | None = typer.Option(
        None,
        "--persona",
        help=(
            "Persona for this run: code, architect, ask, debug, or a custom "
            "persona from .alysis_personas."
        ),
    ),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on agent iterations for each user turn.",
    ),
    subagents: bool | None = typer.Option(
        None,
        "--subagents/--no-subagents",
        help="Enable or disable subagent delegation for this session.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verification command for this chat session (repeatable).",
    ),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands.",
    ),
    diagnostic_log: Path | None = typer.Option(
        None,
        "--diagnostic-log",
        help="Append minimal crash-safe diagnostic events to this JSONL path.",
    ),
    cli_ctx: Any = None,
) -> None:
    from ...llm.types import LLMError

    console = _console()
    requested_path = path
    cfg = load_config()
    effective = clone_cfg(cfg)
    if base_url is not None:
        effective.base_url = base_url
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    current_ctx = cli_ctx if cli_ctx is not None else get_current_context(silent=True)
    non_interactive = _is_non_interactive_terminal()
    stream_source = current_ctx.get_parameter_source("stream") if current_ctx is not None else None
    path_source = current_ctx.get_parameter_source("path") if current_ctx is not None else None
    max_steps_source = (
        current_ctx.get_parameter_source("max_steps") if current_ctx is not None else None
    )
    stream_provided = stream is not None
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        stream_provided = stream_source is not None and stream_source is not ParameterSource.DEFAULT
        max_steps_provided = (
            max_steps is not None
            and max_steps_source is not None
            and max_steps_source is not ParameterSource.DEFAULT
        )
    if stream_provided and stream is not None:
        effective.stream = stream
    else:
        # Interactive chat is more readable with incremental output by default.
        effective.stream = True
    if max_steps is not None:
        effective.max_steps = max_steps
    if subagents is not None:
        effective.subagents_enabled = subagents

    effective_mode = (mode.value if mode else effective.default_mode) or "review"
    delegated_execution = effective.execution.backend == "delegated"

    # The interactive shell is useful even before a selected subscription can
    # serve model calls. Resolve that state separately from model configuration so
    # a disconnected account does not send users back to the classic CLI.
    tui_enabled_now = False
    if not non_interactive:
        try:
            from ..tui import is_tui_enabled as _tui_enabled

            tui_enabled_now = bool(_tui_enabled())
        except Exception:
            tui_enabled_now = False

    # Offer a newer release before any setup/workspace screens. No-op unless a
    # cached check found one; at most once per process (bare `alysis`
    # launches already ran it from the root callback).
    if not non_interactive:
        from ..commands.update import maybe_prompt_update_at_startup

        maybe_prompt_update_at_startup(console=console)

    def _read_subscription_availability() -> Any:
        if delegated_execution:
            return None
        from ..commands.startup import _subscription_availability

        return _subscription_availability(effective)

    subscription_availability = _read_subscription_availability()
    subscription_blocked = bool(
        subscription_availability is not None
        and subscription_availability.active
        and not subscription_availability.ready
    )

    if not delegated_execution and not effective.model and not subscription_blocked:
        should_run_setup = False
        should_run_setup_fn = globals().get("_should_run_first_run_setup_wizard")
        if callable(should_run_setup_fn):
            try:
                should_run_setup = bool(should_run_setup_fn())
            except Exception:
                should_run_setup = False
        setup_fn = globals().get("_maybe_run_first_run_setup_wizard")
        if not non_interactive and should_run_setup and callable(setup_fn):
            console.print("[yellow]No model is configured yet. Starting first-run setup.[/yellow]")
            if not bool(setup_fn()):
                return
            effective = clone_cfg(load_config())
            if base_url is not None:
                effective.base_url = base_url
            if model is not None:
                effective.model = model
            if temperature is not None:
                _apply_temperature_override(effective, temperature)
            if stream_provided and stream is not None:
                effective.stream = stream
            else:
                effective.stream = True
            if max_steps is not None:
                effective.max_steps = max_steps
            if subagents is not None:
                effective.subagents_enabled = subagents
            effective_mode = (mode.value if mode else effective.default_mode) or "review"
            delegated_execution = effective.execution.backend == "delegated"
            subscription_availability = _read_subscription_availability()
            subscription_blocked = bool(
                subscription_availability is not None
                and subscription_availability.active
                and not subscription_availability.ready
            )
        if not delegated_execution and not effective.model and not subscription_blocked:
            _print_chat_missing_model_guidance(console)
            raise typer.Exit(code=2)
    if subscription_blocked and (non_interactive or not tui_enabled_now):
        style = "red" if subscription_availability.is_error else "yellow"
        console.print(f"[{style}]{subscription_availability.message}[/{style}]")
        raise typer.Exit(code=1)
    if delegated_execution and mode is None and str(effective_mode).strip().lower() == "fullaccess":
        effective_mode = "review"
        console.print(
            "[yellow]Delegated runtimes do not inherit the native fullaccess default; "
            "using review (read-only). Pass --mode auto to allow workspace writes.[/yellow]"
        )
    binding_source = _path_binding_source(path_source, requested_path)

    try:
        api_key_override = None
        if delegated_execution:
            from ...agent_runtimes.host import (
                prepare_delegated_runtime,
                run_delegated_chat,
                validate_delegated_cli_options,
            )

            validate_delegated_cli_options(
                base_url=base_url,
                temperature=temperature,
                max_steps=max_steps,
                subagents=subagents,
                verify_cmd=verify_cmd,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
                api_key=api_key,
                stream=stream if stream_provided else None,
                yes=yes,
                diagnostic_log=diagnostic_log,
            )
        else:
            api_key_override = _resolve_api_key_override(
                api_key=api_key,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
            )
        workspace_binding = _resolve_startup_workspace_binding(
            requested_path=requested_path,
            console=console,
            interactive=not non_interactive,
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            source=binding_source,
            action=WorkspaceAction.CHAT,
        )
        session_root = workspace_binding.workspace_context.workspace_root
        focus_path = workspace_binding.workspace_context.focus_path
        if delegated_execution:
            prepare_delegated_runtime(effective, model=model)
            run_delegated_chat(
                cfg=effective,
                cwd=session_root,
                mode=effective_mode,
                initial_images=tuple(path.resolve() for path in (image or ())),
                no_log=no_log,
                console=console,
            )
            return
        if persona is not None:
            # --persona for this invocation: becomes the session's starting
            # persona; the startup machinery applies clamp, scope, overlay,
            # and sticky model exactly like an interactive switch. Unknown
            # names (builtin or custom-from-workspace) are rejected after
            # session creation, where the custom registry is loaded.
            if not persona_modes_enabled(effective):
                console.print("[red]Persona modes are disabled.[/red]")
                raise typer.Exit(code=2)
            effective.default_persona = str(persona).strip().lower()
        # Full-screen TUI is the default interactive chat surface. Users can
        # set ALYSIS_TUI=0 to fall back to the classic prompt loop below.
        if not non_interactive:
            if tui_enabled_now:
                import getpass as _getpass

                from ...git_ops import current_branch as _current_branch
                from ..commands.welcome import _welcome_workspace_value
                from ..tui import TuiState as _TuiState
                from ..tui import next_exec_mode as _next_exec_mode
                from ..tui import run_tui as _run_tui
                from ..tui.app import ConfigReloadOutcome as _TuiConfigReloadOutcome

                try:
                    _username = _getpass.getuser()
                except Exception:
                    _username = ""
                _ws_path = focus_path if focus_path != session_root else session_root
                try:
                    _workspace = _welcome_workspace_value(_ws_path)
                except Exception:
                    _workspace = ""
                try:
                    _branch = _current_branch(session_root)
                except Exception:
                    _branch = ""
                _tui_state = _TuiState(
                    model_name=str(effective.model or ""),
                    connection_status=(
                        "model selection required"
                        if subscription_blocked and subscription_availability.selection_required
                        else "subscription not connected"
                        if subscription_blocked
                        else ""
                    ),
                    username=_username,
                    workspace=_workspace,
                    branch=_branch,
                    exec_mode=str(effective_mode or "").strip(),
                    # No approval-policy field here on purpose: ``exec_mode`` is the
                    # only authority. ``--yes`` keeps its gate-level meaning (it skips
                    # the fs_delete / needs_confirm confirmations inside `auto` mode,
                    # see agent/tools_assembly.py) and deliberately does NOT blanket
                    # auto-allow `review` prompts — that is what `/mode full` is for.
                )
                _tui_box: dict[str, Any] = {"session": None}

                def _tui_session_builder(surface: Any) -> Any:
                    built = create_session(
                        # A /config save that happened before the first message
                        # parks the reloaded config here (see _tui_on_config_saved)
                        # so the lazy build uses it without a full TUI restart.
                        cfg=_tui_box.get("cfg_override") or effective,
                        root=session_root,
                        mode=effective_mode,
                        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
                        yes=yes,
                        max_steps=effective.max_steps,
                        no_log=no_log,
                        api_key_override=api_key_override,
                        console=None,
                        surface=surface,
                        non_interactive=non_interactive,
                        enable_chat_turn_step_budget=True,
                        chat_turn_fixed_override=(
                            effective.max_steps if max_steps_provided else None
                        ),
                        verify_cmd=verify_cmd,
                        subagents_enabled=effective.subagents_enabled,
                        workspace_binding=workspace_binding,
                    )
                    built._alysis_tui_interactive = True
                    _set_chat_usage_hud_enabled(built, _resolve_usage_hud_default(effective))
                    _apply_startup_persona(session=built, console=None)
                    _sync_tui_session_state(_tui_state, built, include_exec_mode=True)
                    _refresh_chat_hud_context_cache(built)
                    _tui_box["session"] = built
                    return built

                def _tui_on_turn_complete() -> None:
                    built = _tui_box.get("session")
                    if built is None:
                        return
                    # An approved switch_mode proposal applies at turn end; the
                    # classic loop does this in its turn-finally, and this hook
                    # is the TUI's turn-end equivalent. Silent on success (the
                    # badge flip is the feedback, matching Tab cycling); the
                    # badge sync below picks up the new persona + mode.
                    try:
                        _apply_pending_persona_switch(session=built, console=None)
                    except Exception:  # noqa: BLE001 - HUD refresh must still run
                        pass
                    _sync_tui_session_state(_tui_state, built, include_exec_mode=True)
                    from ..commands.startup import (
                        _chat_context_percent_value,
                        _format_usage_billing_for_display,
                        _known_cost_value,
                    )

                    try:
                        _refresh_chat_hud_context_cache(built)
                        # The footer reports provider-usable input capacity so
                        # its "context" label agrees with /context and /status.
                        pct = _chat_context_percent_value(built)
                        if pct is not None:
                            _tui_state.context_pct = float(pct)
                    except Exception:
                        pass
                    try:
                        summary = getattr(built, "usage_summary", None)
                        if summary is not None:
                            totals = summary.totals()
                            _tui_state.tokens = int(totals.get("total_tokens") or 0)
                            cost = _known_cost_value(totals)
                            unknown_calls = int(totals.get("unknown_cost_calls") or 0)
                            if cost is not None:
                                _tui_state.cost_usd = float(cost)
                            elif unknown_calls > 0:
                                # Real usage but pricing is unavailable → honest "n/a",
                                # never a fake $0.0000 (e.g. the unmetered MiMo trial).
                                _tui_state.cost_usd = None
                            else:
                                # Nothing metered and nothing unmetered yet → $0.0000.
                                _tui_state.cost_usd = 0.0
                            _tui_state.cost_unknown_calls = unknown_calls
                            _tui_state.cost_display = _format_usage_billing_for_display(
                                totals, compact=True
                            )
                    except Exception:
                        pass
                    _sync_tui_session_state(_tui_state, built)

                def _tui_config_flow_factory() -> Any:
                    # Built fresh each time bare /config opens, so it always reflects
                    # the on-disk config (and any edits applied since launch). The live
                    # session root is passed for display + so "switch project" knows
                    # what it's leaving.
                    from ..tui.config_flow import ConfigFlow

                    built = _tui_box.get("session")
                    root = (
                        str(getattr(built, "root", "") or "")
                        if built is not None
                        else str(session_root)
                    )
                    flow = ConfigFlow(current_workspace=root)
                    if subscription_blocked and subscription_availability.selection_required:
                        flow.open_default_model()
                    return flow

                def _tui_on_config_saved() -> _TuiConfigReloadOutcome:
                    # After the overlay saves, reload the live session from disk so the
                    # new model/keys/limits apply on the next turn (same reload the
                    # classic /config uses), and refresh the footer's model badge.
                    # The explicit outcome lets the TUI stop draining queued work when
                    # this callback has already scheduled a safe session restart.
                    built = _tui_box.get("session")
                    if built is None:
                        # No live session yet (the common launch → /config → save
                        # path): there is nothing to rebuild, so do NOT tear the
                        # TUI down for a slow full restart — hand the freshly
                        # saved config to the lazy session builder and keep the
                        # footer in sync. CLI connection flags stay authoritative.
                        try:
                            from ...config import clone_cfg

                            reloaded = clone_cfg(load_config())
                            if base_url is not None:
                                reloaded.base_url = base_url
                            if model is not None:
                                reloaded.model = model
                            _tui_box["cfg_override"] = reloaded
                            _tui_state.model_name = str(getattr(reloaded, "model", "") or "")
                            return _TuiConfigReloadOutcome.APPLIED
                        except Exception:
                            from prompt_toolkit.application.current import get_app

                            get_app().exit(result=("restart_config", str(session_root)))
                            return _TuiConfigReloadOutcome.RESTART
                    try:
                        from ...config import clone_cfg
                        from ...profiles import connection_fingerprint

                        reloaded = clone_cfg(load_config())
                        # Command-line connection overrides remain authoritative for
                        # this invocation. Reapply them before fingerprinting so a
                        # routing-only save is not mistaken for a persisted connection
                        # change, and before the live reload so /config cannot silently
                        # replace the active CLI model or endpoint.
                        if base_url is not None:
                            reloaded.base_url = base_url
                        if model is not None:
                            reloaded.model = model
                        if connection_fingerprint(reloaded) != connection_fingerprint(built.cfg):
                            from prompt_toolkit.application.current import get_app

                            get_app().exit(
                                result=(
                                    "restart_config",
                                    str(getattr(built, "root", "") or "."),
                                )
                            )
                            return _TuiConfigReloadOutcome.RESTART
                        _apply_config_menu_changes_to_session(session=built, cfg=reloaded)
                        _tui_state.model_name = str(getattr(reloaded, "model", "") or "")
                        new_mode = str(getattr(built, "mode", "") or "").strip()
                        if new_mode:
                            _tui_state.exec_mode = new_mode
                    except _ConfigReloadRequiresRestart:
                        from prompt_toolkit.application.current import get_app

                        get_app().exit(
                            result=(
                                "restart_routing_config",
                                str(getattr(built, "root", "") or "."),
                            )
                        )
                        return _TuiConfigReloadOutcome.RESTART
                    except Exception:
                        return _TuiConfigReloadOutcome.FAILED
                    try:
                        _refresh_chat_hud_context_cache(built)
                    except Exception:
                        pass
                    return _TuiConfigReloadOutcome.APPLIED

                _tui_forge_state = _ForgeChatState()
                _tui_plan_state = _ChatPlanModeState()

                def _tui_make_forge_execute(command_text: str):
                    # Build the callable the worker thread runs for "/execute plan":
                    # it opens the live forge view, runs the swarm (whose events
                    # stream into that view via TuiSurface.on_swarm_event), and
                    # surfaces the handler's captured warnings/summary at the end.
                    def _run(token: Any) -> None:
                        import io as _io

                        from rich.console import Console as _RichConsole

                        built = _tui_box.get("session")
                        if built is None:
                            return
                        surface = getattr(built, "surface", None)
                        paths = getattr(_tui_forge_state, "paths", None)
                        buf = _io.StringIO()
                        cap = _RichConsole(
                            file=buf,
                            force_terminal=False,
                            no_color=True,
                            highlight=False,
                            width=100,
                        )
                        if surface is not None and paths is not None:
                            try:
                                # Pass the worker token so the trace sink's own thread
                                # can drop events after a soft-interrupt.
                                surface.begin_forge(paths, token)
                            except Exception:
                                pass

                        def _report_sink(report_md: Any) -> None:
                            # Render the run's completion answer as a real assistant
                            # markdown block. Deliberately NOT gated on the token: a
                            # soft-interrupt cannot stop run_swarm, so by the time the
                            # handler emits this report the swarm ran to its natural
                            # end — dropping the answer would hide a finished run. The
                            # interrupt is acknowledged with a preface instead.
                            if surface is None:
                                return
                            text = str(report_md or "").strip()
                            if not text:
                                return
                            if bool(getattr(token, "is_cancelled", False)):
                                text = (
                                    "_The interrupt could not stop the running swarm; "
                                    "it ran to completion._\n\n" + text
                                )
                            try:
                                surface.append_note(text, role="assistant")
                            except Exception:
                                pass

                        completed = False
                        try:
                            _handle_chat_command(
                                input_text=command_text,
                                root=focus_path,
                                session=built,
                                pending_images=[],
                                console=cap,
                                forge_state=_tui_forge_state,
                                plan_mode_state=_tui_plan_state,
                                plan_mode_escape_supported=False,
                                forge_execution_report_sink=_report_sink,
                            )
                            completed = True
                        except Exception as _exec_exc:  # noqa: BLE001
                            if surface is not None:
                                try:
                                    surface.append_system(f"Forge execution failed: {_exec_exc}")
                                except Exception:
                                    pass
                        finally:
                            _finalize_deferred_forge_execution(
                                surface=surface,
                                token=token,
                                paths=paths,
                                forge_state=_tui_forge_state,
                                captured=buf.getvalue(),
                                completed=completed,
                            )

                    return _run

                def _tui_make_forge_planner_execute(command_text: str, width: int):
                    """Build the worker job for a plain Forge planner message."""

                    def _run(token: Any) -> None:
                        import io as _io

                        from rich.console import Console as _RichConsole

                        built = _tui_box.get("session")
                        if built is None:
                            return
                        surface = getattr(built, "surface", None)
                        buf = _io.StringIO()
                        # Capture UNWRAPPED: the planmeta renderer reconstructs
                        # logical notes from the captured "│ …" bar lines and
                        # re-wraps them at the live panel width, so wrapping here
                        # would only mangle notes into bar-less continuation
                        # fragments (soft_wrap also stops Rich character-folding
                        # a single giant token, e.g. a provider error blob).
                        cap = _RichConsole(
                            file=buf,
                            force_terminal=False,
                            no_color=True,
                            highlight=False,
                            soft_wrap=True,
                            width=4096,
                        )

                        def _planner_reply_sink(message: Any, questions: Any) -> None:
                            # Render the planner reply as a real assistant markdown
                            # block (styled like any chat turn) instead of dumping the
                            # captured, flat "Planner: …" console text as a system line.
                            if surface is None or getattr(token, "is_cancelled", False):
                                return
                            text = str(message or "").strip()
                            asks = [str(q).strip() for q in (questions or []) if str(q).strip()]
                            parts: list[str] = []
                            if text:
                                parts.append(text)
                            if asks:
                                if parts:
                                    parts.append("")
                                parts.append("**A few questions to sharpen the plan:**")
                                parts.extend(f"- {q}" for q in asks)
                            body = "\n".join(parts).strip()
                            if body:
                                try:
                                    surface.append_note(body, role="assistant")
                                except Exception:
                                    pass

                        try:
                            _handle_chat_command(
                                input_text=command_text,
                                root=focus_path,
                                session=built,
                                pending_images=[],
                                console=cap,
                                forge_state=_tui_forge_state,
                                plan_mode_state=_tui_plan_state,
                                plan_mode_escape_supported=False,
                                forge_planner_reply_sink=_planner_reply_sink,
                            )
                        except Exception as _planner_exc:  # noqa: BLE001
                            if surface is not None and not getattr(token, "is_cancelled", False):
                                try:
                                    surface.append_system(f"Forge planner failed: {_planner_exc}")
                                except Exception:
                                    pass
                            return

                        if surface is None or getattr(token, "is_cancelled", False):
                            return
                        output = buf.getvalue().rstrip("\n")
                        if output:
                            try:
                                # Captured planner meta / plan-reconciliation notes go
                                # into a collapsible dim aside (one line by default,
                                # Ctrl+O to expand) so they never bury the reply.
                                surface.append_note(output, role="planmeta")
                            except Exception:
                                pass

                    return _run

                def _tui_command_runner(
                    sess: Any, text: str, width: int
                ) -> tuple[str, str, str | None, dict[str, Any] | None]:
                    # A plain Forge message with the planner enabled performs a
                    # network/model call. Return a worker job immediately so the
                    # input buffer can repaint cleared and the TUI stays responsive.
                    if _should_defer_forge_planner_submission(
                        forge_state=_tui_forge_state,
                        text=text,
                    ):
                        return (
                            "run",
                            "",
                            text.strip(),
                            {
                                "_deferred_execute": _tui_make_forge_planner_execute(
                                    text.strip(), width
                                )
                            },
                        )
                    # "/execute plan" inside Forge runs the swarm — defer it to the
                    # worker thread (not the synchronous runner, which would freeze
                    # the UI) with a live forge view instead of a flat captured dump.
                    # The bare form is normally intercepted by the launch-gate panel
                    # provider; only its "Launch now" action (a one-shot re-submit of
                    # "/execute plan") reaches here. We normalize to a plain
                    # "/execute plan" (the command handler rejects any other arg); the
                    # chosen knobs travel out-of-band on the forge state.
                    _exec_parts = text.strip().lower().split()
                    if (
                        _tui_forge_state.ui_mode == "forge"
                        and len(_exec_parts) >= 2
                        and _exec_parts[0] == "/execute"
                        and _exec_parts[1] == "plan"
                    ):
                        return (
                            "run",
                            "",
                            "/execute plan",
                            {"_deferred_execute": _tui_make_forge_execute("/execute plan")},
                        )
                    # Typed "/resume <index|id>" applies natively (resolve → swap →
                    # reload transcript) instead of the capture path (whose history
                    # render no-ops with console=None and which can't run the picker).
                    # Bare "/resume" never reaches here — the picker intercepts it.
                    _resume_split = text.strip().split(maxsplit=1)
                    if (
                        len(_resume_split) == 2
                        and _resume_split[0].lower() == "/resume"
                        and _resume_split[1].strip()
                    ):
                        ctx = _tui_resume_context()
                        if ctx is not None:
                            _built_r, sessions_dir_r, current_id_r, ws_r, git_r = ctx
                            raw_arg = _resume_split[1].strip()
                            try:
                                candidates_r = _collect_chat_resume_candidates(
                                    sessions_dir=sessions_dir_r,
                                    current_session_id=current_id_r,
                                    workspace_root=ws_r,
                                    git_root=git_r,
                                )
                            except Exception:  # noqa: BLE001
                                candidates_r = []
                            target_id = _resolve_chat_resume_target(
                                raw_value=raw_arg, sessions=candidates_r
                            )
                            if target_id is None and not raw_arg.isdigit():
                                target_id = _resolve_chat_resume_direct_session_id(
                                    raw_value=raw_arg, sessions_dir=sessions_dir_r
                                )
                            if target_id is None:
                                return (
                                    "handled",
                                    "Invalid session. Use /resume for the picker or "
                                    "/resume <index|session_id>.",
                                    None,
                                    None,
                                )
                            # _tui_resume_apply writes the outcome (and reloads the
                            # transcript) itself, so return empty output to avoid a
                            # duplicate line.
                            _tui_resume_apply(target_id)
                            return ("handled", "", None, None)
                    # Route a submission through the chat command handler with a
                    # capture console so its output renders into the TUI transcript
                    # instead of corrupting the alt-screen. stdin is swapped to EOF
                    # so any interactive command (e.g. /resume) cancels cleanly.
                    import io as _io
                    import sys as _sys

                    from rich.console import Console as _RichConsole

                    buf = _io.StringIO()
                    cap = _RichConsole(
                        file=buf,
                        force_terminal=False,
                        no_color=True,
                        highlight=False,
                        width=max(20, min(int(width or 100), 120)),
                    )
                    saved_stdin = _sys.stdin
                    plan_mode_action_prompt = None
                    surface_for_plan = getattr(sess, "surface", None)
                    defer_plan_mode_approval = getattr(
                        surface_for_plan, "defer_plan_mode_approval", None
                    )
                    if callable(defer_plan_mode_approval):

                        def _tui_plan_mode_action_prompt(
                            *, console: Any, user_message: str, draft: str
                        ) -> str | None:
                            console.print(_plan_mode_actions_panel())
                            console.print("Select option [1/2/3]:")
                            defer_plan_mode_approval(
                                user_message=user_message,
                                draft=draft,
                                approved_instruction=instruction_with_approved_plan(
                                    user_message=user_message,
                                    approved_plan=draft,
                                ),
                            )
                            return None

                        plan_mode_action_prompt = _tui_plan_mode_action_prompt
                    try:
                        _sys.stdin = _io.StringIO("")
                        result = _handle_chat_command(
                            input_text=text,
                            root=focus_path,
                            session=sess,
                            pending_images=[],
                            console=cap,
                            forge_state=_tui_forge_state,
                            plan_mode_state=_tui_plan_state,
                            plan_mode_escape_supported=False,
                            plan_mode_action_prompt=plan_mode_action_prompt,
                        )
                    except Exception as _cmd_exc:  # noqa: BLE001
                        return ("handled", f"Command error: {_cmd_exc}", None, None)
                    finally:
                        _sys.stdin = saved_stdin
                    output = buf.getvalue().rstrip("\n")
                    # Keep footer badges in sync after local commands such as
                    # /mode and /usage hud.
                    _sync_tui_session_state(_tui_state, sess, include_exec_mode=True)
                    # The forge state machine flips ui_mode inside _enter_forge_mode
                    # (and back to "chat" on /back / /done); this single sync drives
                    # the FORGE footer badge + the forge-specific input placeholder.
                    _tui_state.forge_mode = _tui_forge_state.ui_mode == "forge"
                    if _tui_state.forge_mode:
                        _forge_paths = getattr(_tui_forge_state, "paths", None)
                        _run_id = str(getattr(_forge_paths, "run_id", "") or "")
                        _tui_state.forge_run_id = (
                            _run_id if len(_run_id) <= 12 else _run_id[:11] + "…"
                        )
                    else:
                        _tui_state.forge_run_id = ""
                    if result == "exit":
                        return ("exit", output, None, None)
                    if result == "send":
                        return ("run", output, text, {})
                    if isinstance(result, _ChatExecutionRequest):
                        run_kwargs: dict[str, Any] = {}
                        if result.routing_mode_override:
                            run_kwargs["routing_mode_override"] = result.routing_mode_override
                        if result.ephemeral_system_messages:
                            run_kwargs["ephemeral_system_messages"] = list(
                                result.ephemeral_system_messages
                            )
                        if result.ephemeral_user_messages:
                            run_kwargs["ephemeral_user_messages"] = list(
                                result.ephemeral_user_messages
                            )
                        return ("run", output, result.instruction, run_kwargs)
                    return ("handled", output, None, None)

                from ..commands.chat_status import _chat_status_panel_spec
                from ..commands.chat_tui_panels import (
                    _chat_asset_detail_panel_spec,
                    _chat_assets_picker_spec,
                    _chat_config_panel_spec,
                    _chat_context_panel_spec,
                    _chat_forge_intro_panel_spec,
                    _chat_forge_markdown_panel_spec,
                    _chat_forge_plan_panel_spec,
                    _chat_model_info_panel_spec,
                    _chat_skill_listing_panel_spec,
                    _chat_terminals_panel_spec,
                    _chat_toolbar_panel_spec,
                    _chat_usage_panel_spec,
                )

                def _tui_status_panel(arg: str = "") -> dict[str, Any] | None:
                    # TUI-native /status: render the live session snapshot as a
                    # centered popup instead of a flat gray table dump.
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_status_panel_spec(session=built, pending_images=[])
                    except Exception:  # noqa: BLE001 - never crash the UI
                        return None

                def _tui_usage_panel(arg: str = "") -> dict[str, Any] | None:
                    # Bare /usage opens the panel; "/usage hud …" falls through to
                    # the command runner so the HUD toggle still applies.
                    if arg.strip():
                        return None
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_usage_panel_spec(session=built)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_context_panel(arg: str = "") -> dict[str, Any] | None:
                    if arg.strip():
                        return None
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_context_panel_spec(session=built)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_model_info_panel(arg: str = "") -> dict[str, Any] | None:
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_model_info_panel_spec(session=built, model_ref=arg)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_config_panel(arg: str = "") -> dict[str, Any] | None:
                    # Bare /config now opens the full interactive menu overlay (handled
                    # in app._submit before this provider). This read-only model panel
                    # serves /config show|list|help (the tracked-model view) and acts as
                    # the fallback when the overlay is unavailable; set|clear|rm|delete
                    # fall through to the command runner so edits still apply.
                    normalized = arg.strip().lower()
                    if normalized and normalized not in {"show", "list", "help"}:
                        return None
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_config_panel_spec(session=built)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_toolbar_panel(arg: str = "") -> dict[str, Any] | None:
                    if arg.strip():
                        return None
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_toolbar_panel_spec(session=built)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_terminals_panel(arg: str = "") -> dict[str, Any] | None:
                    # Bare /terminals (or "list") opens the panel; show|kill|help fall
                    # through to the command runner.
                    normalized = arg.strip().lower()
                    if normalized and normalized != "list":
                        return None
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_terminals_panel_spec(session=built)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_skill_panel(arg: str = "") -> dict[str, Any] | None:
                    # Bare /skill lists skills; "/skill <name>" and "/skill <name>
                    # <task>" fall through to the runner (info text / attach turn).
                    if arg.strip():
                        return None
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    try:
                        return _chat_skill_listing_panel_spec(session=built)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_forge_planner_choice(value: Any) -> dict[str, Any]:
                    # A pick from the "Use the planner?" prompt → enter Forge with the
                    # plan assistant forced on/off. Submitting the flagged /forge form
                    # runs the full entry pipeline (workspace binding + footer sync);
                    # the flag is parsed into planner_assistant_default, never a goal.
                    choice = str(value or "").strip().lower()
                    submit = "/forge --planner" if choice == "yes" else "/forge --no-planner"
                    return {"submit": submit}

                def _tui_forge_planner_prompt() -> dict[str, Any]:
                    # on_confirm for the /forge intro popup: instead of entering Forge
                    # directly, open a native picker asking whether to use the planner
                    # assistant (the classic stdin prompt can't run in the alt-screen).
                    return {
                        "picker": {
                            "title": "Use the planner assistant?",
                            "hint": "↑↓ move · Enter select · Esc cancel",
                            "rows": [
                                {
                                    "value": "yes",
                                    "label": "Yes — use the planner assistant",
                                    "description": (
                                        "Draft and refine the plan together before building."
                                    ),
                                    "current": True,
                                },
                                {
                                    "value": "no",
                                    "label": "No — plan it myself",
                                    "description": (
                                        "Enter Forge and shape the plan with /goal and /task."
                                    ),
                                },
                            ],
                            "on_select": _tui_forge_planner_choice,
                        }
                    }

                def _tui_forge_intro_panel(arg: str = "") -> dict[str, Any] | None:
                    # Plain /forge opens a guidance popup explaining how Forge works.
                    # Its Enter runs on_confirm, which opens the native "Use the
                    # planner?" picker; the pick enters Forge with the assistant on/off.
                    # "/forge <goal>" / "/forge resume" (arg present) and re-entry while
                    # already in Forge return None so they fall through to the runner
                    # and enter directly (no intro, no planner prompt).
                    if arg.strip():
                        return None
                    if _tui_forge_state.ui_mode == "forge":
                        return None
                    try:
                        spec = _chat_forge_intro_panel_spec()
                    except Exception:  # noqa: BLE001 - never crash the UI on a panel
                        return None
                    # Swap the bare confirm-command for the planner prompt so the user
                    # chooses the planner natively before Forge actually enters.
                    spec.pop("confirm", None)
                    spec["on_confirm"] = _tui_forge_planner_prompt
                    spec["hint"] = "↵ Enter — choose planner   ·   Esc — cancel"
                    return spec

                # --- Forge read-only panels (only fire inside a Forge session) ---
                def _tui_forge_plan_and_paths() -> tuple[Any, Any] | None:
                    if _tui_forge_state.ui_mode != "forge":
                        return None  # chat-mode /plan, /show handled elsewhere
                    plan = getattr(_tui_forge_state, "plan", None)
                    paths = getattr(_tui_forge_state, "paths", None)
                    if not isinstance(plan, dict) or paths is None:
                        return None
                    return plan, paths

                def _tui_forge_show_panel(arg: str = "") -> dict[str, Any] | None:
                    # Forge /show → the plan summary panel.
                    pair = _tui_forge_plan_and_paths()
                    if pair is None:
                        return None
                    plan, paths = pair
                    try:
                        return _chat_forge_plan_panel_spec(paths=paths, plan=plan)
                    except Exception:  # noqa: BLE001 - never crash the UI on a panel
                        return None

                def _tui_forge_plan_edit_spec(plan: Any, paths: Any) -> dict[str, Any]:
                    # /plan edit → open plan.json in the in-TUI editor; on save,
                    # validate the JSON shape, persist it, and reload the in-memory
                    # plan (no $EDITOR, which can't run under the alt-screen).
                    import json as _json

                    from ...forge import save_plan as _save_plan

                    try:
                        _save_plan(paths, plan)  # PLAN.md/json reflect the live plan
                    except Exception:
                        pass
                    try:
                        text = paths.plan_json_path.read_text(encoding="utf-8")
                    except Exception:
                        text = _json.dumps(plan, indent=2, ensure_ascii=False)

                    def _on_save(new_text: str) -> tuple[bool, str]:
                        try:
                            candidate = _json.loads(new_text)
                        except _json.JSONDecodeError as exc:
                            return (False, f"Invalid JSON: {exc}")
                        from ..commands.forge_helpers import _validate_forge_plan_shape

                        shape_error = _validate_forge_plan_shape(candidate)
                        if shape_error:
                            return (False, shape_error)
                        try:
                            _save_plan(paths, candidate)
                        except Exception as exc:  # noqa: BLE001
                            return (False, f"Failed to save plan: {exc}")
                        _tui_forge_state.plan = candidate
                        suffix = ""
                        try:
                            from ..commands.forge_helpers import (
                                _validate_forge_plan_for_paths,
                            )

                            warnings = _validate_forge_plan_for_paths(paths, candidate)
                            if warnings:
                                suffix = f" · {len(warnings)} validation warning(s)"
                        except Exception:
                            pass
                        return (True, f"Plan saved and reloaded.{suffix}")

                    return {
                        "editor": {
                            "title": f"Edit plan.json · {getattr(paths, 'run_id', '')}".rstrip(
                                " ·"
                            ),
                            "text": text,
                            "on_save": _on_save,
                        }
                    }

                def _tui_forge_plan_panel(arg: str = "") -> dict[str, Any] | None:
                    # Forge /plan: tasks|table|view → plan panel; markdown|md → PLAN.md
                    # doc panel; edit → in-TUI JSON editor. Bare /plan returns None so
                    # it falls through to the forge /plan picker.
                    pair = _tui_forge_plan_and_paths()
                    if pair is None:
                        return None
                    plan, paths = pair
                    sub = arg.strip().lower()
                    try:
                        if sub in {"tasks", "table", "view"}:
                            return _chat_forge_plan_panel_spec(paths=paths, plan=plan)
                        if sub in {"markdown", "md"}:
                            return _chat_forge_markdown_panel_spec(paths=paths, plan=plan)
                        if sub in {"edit", "edit-json"}:
                            return _tui_forge_plan_edit_spec(plan, paths)
                    except Exception:  # noqa: BLE001
                        return None
                    return None

                # --- Forge assets (picker + detail panel; replaces the stdin modal) ---
                def _tui_asset_context() -> tuple[Any, Any] | None:
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    cfg = getattr(built, "cfg", None)
                    if not isinstance(cfg, AppConfig):
                        cfg = effective if isinstance(effective, AppConfig) else None
                    asset_paths = getattr(_tui_forge_state, "paths", None)
                    if asset_paths is None:
                        # Outside a Forge session, resolve the workspace's current run.
                        try:
                            from ...agent.prompt_context import (
                                resolve_session_active_workdir_path,
                            )
                            from ...forge import load_current_run_paths

                            asset_paths = load_current_run_paths(
                                Path(resolve_session_active_workdir_path(built))
                            )
                        except Exception:
                            asset_paths = None
                    if cfg is None or asset_paths is None:
                        return None
                    return cfg, asset_paths

                def _tui_assets_panel(arg: str = "") -> dict[str, Any] | None:
                    ctx = _tui_asset_context()
                    if ctx is None:
                        return None
                    cfg_obj, asset_paths = ctx
                    asset_id = arg.strip()
                    if asset_id:
                        try:
                            return _chat_asset_detail_panel_spec(
                                cfg=cfg_obj, paths=asset_paths, asset_id=asset_id
                            )
                        except Exception:  # noqa: BLE001
                            return None
                    # Bare /assets: show a small panel only when there is nothing to
                    # pick; otherwise fall through to the selectable picker below.
                    try:
                        from ...assets.surface import build_asset_surface

                        count = len(
                            build_asset_surface(cfg=cfg_obj, run_paths=asset_paths).list_assets()
                        )
                    except Exception:  # noqa: BLE001
                        return None
                    if count == 0:
                        return {
                            "title": "Assets",
                            "sections": [
                                (
                                    "Assets",
                                    [("status", "No assets attached to this run yet.", "plain")],
                                )
                            ],
                            "hint": "Esc close",
                        }
                    return None

                def _tui_assets_picker() -> dict[str, Any] | None:
                    ctx = _tui_asset_context()
                    if ctx is None:
                        return None
                    cfg_obj, asset_paths = ctx
                    try:
                        return _chat_assets_picker_spec(cfg=cfg_obj, paths=asset_paths)
                    except Exception:  # noqa: BLE001
                        return None

                def _tui_login_select(value: str) -> dict[str, Any]:
                    return {"exit": ("login_connection", str(value))}

                def _tui_login_picker() -> dict[str, Any]:
                    from ..commands.auth import login_connection_rows

                    rows = [
                        {
                            "label": label,
                            "description": description,
                            "value": connection_id,
                        }
                        for connection_id, label, description in login_connection_rows()
                    ]
                    return {
                        "title": "Log in",
                        "hint": "Choose how Alysis Code should connect",
                        "rows": rows,
                        "on_select": _tui_login_select,
                    }

                # --- Forge launch gate: /execute plan opens a confirm + knob picker ---
                # One-shot flag: "Launch now" sets it, then re-submits a clean
                # "/execute plan"; the gate provider consumes it and falls through to
                # the runner, so the swarm launches without echoing any internal
                # sentinel into the visible transcript.
                _tui_forge_launch = {"confirmed": False}
                _FORGE_KNOB_DEFAULTS: dict[str, Any] = {
                    "parallel": 2,
                    "scope_mode": "strict",
                    "verify_mode": "warn",
                    "review": False,
                }

                def _tui_forge_knobs() -> dict[str, Any]:
                    knobs = dict(_FORGE_KNOB_DEFAULTS)
                    stored = _tui_forge_state.swarm_knobs
                    if isinstance(stored, dict):
                        knobs.update(stored)
                    return knobs

                def _tui_forge_cycle_knob(key: str) -> None:
                    knobs = _tui_forge_knobs()
                    if key == "parallel":
                        current = int(knobs.get("parallel", 2) or 2)
                        knobs["parallel"] = current + 1 if current < 4 else 1
                    elif key == "scope_mode":
                        knobs["scope_mode"] = (
                            "warn" if knobs.get("scope_mode") == "strict" else "strict"
                        )
                    elif key == "verify_mode":
                        order = ["warn", "strict", "off"]
                        try:
                            idx = order.index(str(knobs.get("verify_mode")))
                        except ValueError:
                            idx = 0
                        knobs["verify_mode"] = order[(idx + 1) % len(order)]
                    elif key == "review":
                        knobs["review"] = not bool(knobs.get("review"))
                    _tui_forge_state.swarm_knobs = knobs

                def _tui_forge_knob_blurb(key: str, knobs: dict[str, Any]) -> str:
                    # One terse line describing only the *currently selected* value —
                    # cycling a knob swaps the line, so each alternative explains
                    # itself the moment it is picked (never an all-values glossary).
                    if key == "parallel":
                        n = int(knobs.get("parallel", 2) or 2)
                        if n <= 1:
                            return "Tasks build one at a time."
                        return f"Up to {n} tasks build at once."
                    if key == "scope_mode":
                        if knobs.get("scope_mode") == "strict":
                            return "A worker edits only its task's files."
                        return "Cross-task edits allowed, just flagged in the report."
                    if key == "verify_mode":
                        # NB: warn still fails a task on NEW check failures — it only
                        # downgrades pre-existing/baseline failures and infra problems
                        # (see swarm_worker._run_authoritative_verification).
                        mode = str(knobs.get("verify_mode"))
                        if mode == "strict":
                            return "Any check failure fails the task."
                        if mode == "off":
                            return "Skip verification checks."
                        return "New failures fail the task; pre-existing just warn."
                    if key == "review":
                        if knobs.get("review"):
                            return "A reviewer checks each task before it merges."
                        return "Merge without a reviewer pass."
                    return ""

                def _tui_forge_launch_gate_picker(
                    plan: Any, paths: Any, focus: str | None = None
                ) -> dict[str, Any]:
                    knobs = _tui_forge_knobs()
                    tasks = plan.get("tasks") if isinstance(plan, dict) else None
                    n_tasks = len(tasks) if isinstance(tasks, list) else 0
                    run_id = str(getattr(paths, "run_id", "") or "")
                    all_default = all(
                        knobs.get(key) == value for key, value in _FORGE_KNOB_DEFAULTS.items()
                    )

                    def _default_tag(key: str) -> str:
                        if knobs.get(key) == _FORGE_KNOB_DEFAULTS.get(key):
                            return "(default)"
                        return ""

                    rows = [
                        {
                            "value": "__launch__",
                            "label": "Launch now",
                            "description": (
                                f"Build {n_tasks} task(s) — recommended defaults."
                                if all_default
                                else f"Build {n_tasks} task(s) with the settings below."
                            ),
                            "current": focus in (None, "__launch__"),
                            "tag": "",
                        },
                        {
                            "value": "parallel",
                            "label": f"Workers: {knobs['parallel']}",
                            "description": _tui_forge_knob_blurb("parallel", knobs),
                            "current": focus == "parallel",
                            "tag": _default_tag("parallel"),
                        },
                        {
                            "value": "scope_mode",
                            "label": f"File scope: {knobs['scope_mode']}",
                            "description": _tui_forge_knob_blurb("scope_mode", knobs),
                            "current": focus == "scope_mode",
                            "tag": _default_tag("scope_mode"),
                        },
                        {
                            "value": "verify_mode",
                            "label": f"Verify: {knobs['verify_mode']}",
                            "description": _tui_forge_knob_blurb("verify_mode", knobs),
                            "current": focus == "verify_mode",
                            "tag": _default_tag("verify_mode"),
                        },
                        {
                            "value": "review",
                            "label": f"Review: {'on' if knobs['review'] else 'off'}",
                            "description": _tui_forge_knob_blurb("review", knobs),
                            "current": focus == "review",
                            "tag": _default_tag("review"),
                        },
                    ]

                    def _on_select(value: Any) -> dict[str, Any] | None:
                        selected = str(value)
                        if selected == "__launch__":
                            # Confirm-launch: flag a one-shot bypass, then re-submit a
                            # clean "/execute plan" (no sentinel echoed to the
                            # transcript). The gate provider consumes the flag and the
                            # runner defers the real swarm execution on the worker.
                            _tui_forge_launch["confirmed"] = True
                            return {"submit": "/execute plan"}
                        _tui_forge_cycle_knob(selected)
                        pair = _tui_forge_plan_and_paths()
                        if pair is None:
                            return None
                        # Re-present the gate with the cycled value, keeping focus.
                        return {
                            "picker": _tui_forge_launch_gate_picker(
                                pair[0], pair[1], focus=selected
                            )
                        }

                    title = (f"Launch Forge · {run_id}".rstrip(" ·")) or "Launch Forge"
                    return {
                        "title": title,
                        "hint": "↑↓ move · Enter/1 launch · 2-5 cycle setting · Esc cancel",
                        "rows": rows,
                        "on_select": _on_select,
                    }

                def _tui_forge_execute_gate(arg: str = "") -> dict[str, Any] | None:
                    # /execute plan → open the launch gate instead of firing the swarm
                    # blind. Only the bare "plan" form opens it; any other arg returns
                    # None to fall through to the runner.
                    if _tui_forge_state.ui_mode != "forge":
                        return None
                    if arg.strip().lower() != "plan":
                        return None
                    if _tui_forge_launch.get("confirmed"):
                        # The gate's own "Launch now" re-submitting "/execute plan":
                        # consume the one-shot flag and fall through so the runner
                        # defers the real execution (no second gate, no loop).
                        _tui_forge_launch["confirmed"] = False
                        return None
                    pair = _tui_forge_plan_and_paths()
                    if pair is None:
                        return None
                    plan, paths = pair
                    # No usable plan yet → let the runner surface the "add tasks first"
                    # guidance rather than offering a launch that will be rejected.
                    tasks = plan.get("tasks") if isinstance(plan, dict) else None
                    reqs = plan.get("requirements") if isinstance(plan, dict) else None
                    if not (isinstance(tasks, list) and tasks) and not (
                        isinstance(reqs, list) and reqs
                    ):
                        return None
                    return {"picker": _tui_forge_launch_gate_picker(plan, paths)}

                _tui_panel_providers = {
                    "/status": _tui_status_panel,
                    "/usage": _tui_usage_panel,
                    "/ctx": _tui_context_panel,
                    "/context": _tui_context_panel,
                    "/model-info": _tui_model_info_panel,
                    "/config": _tui_config_panel,
                    "/toolbar": _tui_toolbar_panel,
                    "/terminals": _tui_terminals_panel,
                    "/skill": _tui_skill_panel,
                    "/forge": _tui_forge_intro_panel,
                    "/show": _tui_forge_show_panel,
                    "/plan": _tui_forge_plan_panel,
                    "/assets": _tui_assets_panel,
                    "/execute": _tui_forge_execute_gate,
                }

                # Slash-command dropdown: same completer the classic prompt uses, so
                # typing "/" lists commands and /skill completes registered names.
                from ..chat_slash_completer import ChatSlashCompleter

                def _tui_skill_names() -> list[str]:
                    built = _tui_box.get("session")
                    return sorted(str(n) for n in (getattr(built, "skill_registry", {}) or {}))

                _tui_completer = ChatSlashCompleter(
                    mode_provider=lambda: _tui_forge_state.ui_mode,
                    skill_names_provider=_tui_skill_names,
                )

                # TUI-native /mode picker: bare /mode opens a selectable popup;
                # "/mode <name>" still applies inline via the command runner.
                from ..commands.chat_terminal import (
                    _chat_mode_display,
                    _chat_mode_rows,
                    _chat_persona_rows,
                    _chat_trace_rows,
                )
                from ..commands.startup import (
                    _chat_trace_level,
                    _set_chat_stream_enabled,
                    _set_chat_trace_level,
                )

                _FULLACCESS_WARNING = (
                    "full (fullaccess) disables write/shell safety guards and approval prompts."
                )

                def _tui_mode_select(value: str) -> list[tuple[str, str]] | None:
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    current = str(getattr(built, "mode", "review") or "review").strip().lower()
                    if _chat_plan_mode_enabled(_tui_plan_state):
                        if value == "readonly":
                            return [("system", "Mode already set: Read-Only (Plan Mode is on)")]
                        return [
                            (
                                "warn",
                                "Cannot change execution mode while Plan Mode is on. "
                                "Use /plan off first.",
                            )
                        ]
                    if value == current:
                        msgs = [("system", f"Mode already set: {_chat_mode_display(value)}")]
                        if value == "fullaccess":
                            msgs.append(("warn", _FULLACCESS_WARNING))
                        return msgs
                    try:
                        _clear_persona_restore(built)
                        _apply_chat_effective_mode(
                            session=built, next_mode=value, persist_default_mode=True
                        )
                    except Exception as exc:  # noqa: BLE001
                        return [("error", f"Failed to change mode: {exc}")]
                    _tui_state.exec_mode = value
                    msgs = [("system", f"Mode → {_chat_mode_display(value)}")]
                    if value == "fullaccess":
                        msgs.append(("warn", _FULLACCESS_WARNING))
                    return msgs

                def _tui_persona_select(value: str) -> list[tuple[str, str]] | None:
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    if not persona_modes_enabled(getattr(built, "cfg", None)):
                        return [("warn", "Persona modes are disabled.")]
                    if _chat_plan_mode_enabled(_tui_plan_state):
                        return [
                            (
                                "warn",
                                "Cannot change persona while Plan Mode is on. Use /plan off first.",
                            )
                        ]
                    registry = _session_persona_registry(built)
                    persona_name = normalize_persona(value, registry)
                    current_persona = normalize_persona(getattr(built, "persona", "code"), registry)
                    if persona_name == current_persona:
                        return [("system", f"Persona already set: {persona_name}")]
                    try:
                        _apply_chat_persona(session=built, persona=persona_name, source="user")
                    except Exception as exc:  # noqa: BLE001
                        return [("error", f"Failed to change persona: {exc}")]
                    _tui_state.persona = persona_name
                    _tui_state.exec_mode = str(getattr(built, "mode", "") or "").strip()
                    return [("system", f"Persona → {persona_name}")]

                def _tui_persona_picker() -> dict[str, Any] | None:
                    built = _tui_box.get("session")
                    registry = _session_persona_registry(built)
                    current_persona = normalize_persona(getattr(built, "persona", "code"), registry)
                    rows: list[dict[str, Any]] = []
                    for value, label, desc in _chat_persona_rows(custom=registry):
                        clean = label.split(") ", 1)[-1] if ") " in label else label
                        rows.append(
                            {
                                "label": clean,
                                "description": desc,
                                "value": value,
                                "current": value == current_persona,
                            }
                        )
                    return {
                        "title": "Persona",
                        "rows": rows,
                        "on_select": _tui_persona_select,
                    }

                def _tui_persona_cycle() -> list[tuple[str, str]] | None:
                    # Kilo/OpenCode-style shortcut: Tab on an empty input cycles
                    # code -> architect -> ask -> debug -> code. Same primitive,
                    # clamp, and events as /mode <persona>. Success is SILENT:
                    # the footer badge flipping is the feedback — the transcript
                    # only ever sees warnings and errors.
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    if not persona_modes_enabled(getattr(built, "cfg", None)):
                        return None
                    if _chat_plan_mode_enabled(_tui_plan_state):
                        return [
                            (
                                "warn",
                                "Cannot change persona while Plan Mode is on. Use /plan off first.",
                            )
                        ]
                    target = next_persona(
                        getattr(built, "persona", "code"), _session_persona_registry(built)
                    )
                    try:
                        effective = _apply_chat_persona(
                            session=built, persona=target, source="user"
                        )
                    except Exception as exc:  # noqa: BLE001
                        return [("error", f"Failed to change persona: {exc}")]
                    _tui_state.persona = target
                    _tui_state.exec_mode = effective
                    return None

                def _tui_mode_cycle() -> list[tuple[str, str]] | None:
                    # Shift+Tab shortcut: advance the execution mode one notch
                    # (read -> safe -> fast -> full -> read). Routed through
                    # _tui_mode_select so it shares the /mode picker's plan-mode
                    # guard, persistence, and fullaccess warning — the mode is the
                    # only approval authority, so this must not be a footer-only
                    # flip. Unlike the persona cycle this is NOT silent: landing on
                    # fullaccess has to announce itself.
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    current = str(getattr(built, "mode", "review") or "review").strip().lower()
                    return _tui_mode_select(_next_exec_mode(current))

                def _tui_mode_picker() -> dict[str, Any] | None:
                    built = _tui_box.get("session")
                    current = str(getattr(built, "mode", "review") or "review").strip().lower()
                    rows: list[dict[str, Any]] = []
                    for value, label, desc in _chat_mode_rows():
                        clean = label.split(") ", 1)[-1] if ") " in label else label
                        rows.append(
                            {
                                "label": clean,
                                "description": desc,
                                "value": value,
                                "current": value == current,
                            }
                        )
                    return {"title": "Mode", "rows": rows, "on_select": _tui_mode_select}

                # TUI-native /stream picker: bare /stream opens a selectable popup;
                # "/stream on|off|status" still applies inline via the command runner.
                def _tui_stream_select(value: str) -> list[tuple[str, str]] | None:
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    enabled = value == "on"
                    current = bool(getattr(built, "stream", True))
                    if enabled == current:
                        return [("system", f"Streaming already set: {value}")]
                    _set_chat_stream_enabled(session=built, enabled=enabled)
                    return [("system", f"Streaming → {value}")]

                def _tui_stream_picker() -> dict[str, Any] | None:
                    built = _tui_box.get("session")
                    enabled = bool(getattr(built, "stream", True)) if built is not None else True
                    return {
                        "title": "Streaming",
                        "rows": [
                            {
                                "label": "on",
                                "description": (
                                    "Render answers and available safe reasoning summaries live "
                                    "(recommended)."
                                ),
                                "value": "on",
                                "current": enabled,
                            },
                            {
                                "label": "off",
                                "description": (
                                    "Buffer answers and summaries until each response completes."
                                ),
                                "value": "off",
                                "current": not enabled,
                            },
                        ],
                        "on_select": _tui_stream_select,
                    }

                # TUI-native /trace picker: bare /trace opens a selectable popup;
                # "/trace <level>" still applies inline via the command runner.
                def _tui_trace_select(value: str) -> list[tuple[str, str]] | None:
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    current = _chat_trace_level(built)
                    if value == current:
                        return [("system", f"Reasoning trace already set: {value}")]
                    try:
                        applied = _set_chat_trace_level(session=built, level=value)
                    except Exception as exc:  # noqa: BLE001
                        return [("error", f"Failed to set trace level: {exc}")]
                    return [("system", f"Reasoning trace → {applied}")]

                def _tui_trace_picker() -> dict[str, Any] | None:
                    built = _tui_box.get("session")
                    current = _chat_trace_level(built) if built is not None else "compact"
                    rows: list[dict[str, Any]] = []
                    for value, label, desc in _chat_trace_rows():
                        clean = label.split(") ", 1)[-1] if ") " in label else label
                        rows.append(
                            {
                                "label": clean,
                                "description": desc,
                                "value": value,
                                "current": value == current,
                            }
                        )
                    return {
                        "title": "Reasoning Trace",
                        "rows": rows,
                        "on_select": _tui_trace_select,
                    }

                # TUI-native forge pickers: bare /plan opens a tasks/markdown/edit
                # chooser; bare /assistant toggles the planner assistant. Each row's
                # on_select SUBMITS the explicit form (e.g. "/plan tasks") so the
                # panel/runner path applies with no extra Enter. They fire only in a
                # Forge session; in chat mode they return None and fall through.
                def _tui_forge_plan_picker() -> dict[str, Any] | None:
                    if _tui_forge_state.ui_mode != "forge":
                        return None
                    rows = [
                        {
                            "label": "tasks",
                            "description": "Show the plan summary — goal, requirements, task table.",
                            "value": "tasks",
                            "current": False,
                        },
                        {
                            "label": "markdown",
                            "description": "Preview PLAN.md for the current run.",
                            "value": "markdown",
                            "current": False,
                        },
                        {
                            "label": "edit",
                            "description": "Edit plan.json in an in-TUI editor, then reload.",
                            "value": "edit",
                            "current": False,
                        },
                    ]
                    return {
                        "title": "Plan",
                        "rows": rows,
                        "on_select": lambda value: {"submit": f"/plan {value}"},
                        "hint": "↑↓ select · Enter · Esc cancel",
                    }

                def _tui_forge_assistant_picker() -> dict[str, Any] | None:
                    if _tui_forge_state.ui_mode != "forge":
                        return None
                    enabled = bool(getattr(_tui_forge_state, "assistant_enabled", False))
                    rows = [
                        {
                            "label": "on",
                            "description": "Planner assistant drafts and refines the plan with you.",
                            "value": "on",
                            "current": enabled,
                        },
                        {
                            "label": "off",
                            "description": "Manual planning only — you drive /goal, /task, /plan edit.",
                            "value": "off",
                            "current": not enabled,
                        },
                    ]
                    return {
                        "title": "Planner Assistant",
                        "rows": rows,
                        "on_select": lambda value: {"submit": f"/assistant {value}"},
                        "hint": "↑↓ select · Enter · Esc cancel",
                    }

                # TUI-native /resume picker: bare /resume opens a selectable list of
                # previous sessions (label = relative time, description = preview);
                # choosing one swaps the live session in place and reloads the prior
                # conversation into the transcript so the user "enters" it. The typed
                # form "/resume <index|id>" is intercepted in the command runner so it
                # applies natively too (no flat capture dump).
                from ...session_store import resolve_sessions_dir
                from ..commands.chat_resume_helpers import (
                    _chat_resume_picker_spec,
                    _collect_chat_resume_candidates,
                    _resolve_chat_resume_direct_session_id,
                    _resolve_chat_resume_target,
                    _resume_chat_session,
                )

                def _tui_resume_context() -> tuple[Any, Path, str, Any, Any] | None:
                    # (live session, sessions dir, current session id, current
                    # workspace_root, current git_root) or None when there is no
                    # session / no resolvable sessions directory. The workspace
                    # roots scope the candidate list so /resume only shows this
                    # workspace's chats.
                    built = _tui_box.get("session")
                    if built is None:
                        return None
                    store = getattr(built, "store", None)
                    sessions_dir_raw = getattr(store, "sessions_dir", None)
                    if sessions_dir_raw is not None:
                        sessions_dir = Path(sessions_dir_raw)
                    else:
                        cfg = getattr(built, "cfg", None)
                        if not isinstance(cfg, AppConfig):
                            return None
                        sessions_dir = resolve_sessions_dir(cfg)
                    return (
                        built,
                        sessions_dir,
                        str(getattr(store, "session_id", "") or ""),
                        getattr(store, "workspace_root", None),
                        getattr(store, "git_root", None),
                    )

                def _tui_resume_apply(target_session_id: str) -> None:
                    # Swap the live session to the chosen one (mutated in place, so the
                    # app's session reference + surface stay valid), reload its history
                    # into the transcript, and refresh the footer HUD/model/mode. Writes
                    # the outcome line straight to the transcript (with a role chosen to
                    # stay visible) rather than returning it, so the picker and the typed
                    # paths behave identically.
                    built = _tui_box.get("session")
                    surface = getattr(built, "surface", None) if built is not None else None

                    def _note(text: str, role: str = "system") -> None:
                        appender = getattr(surface, "append_note", None)
                        if callable(appender):
                            try:
                                appender(text, role=role)
                            except Exception:  # noqa: BLE001 - feedback is best-effort
                                pass

                    if built is None:
                        return
                    store = getattr(built, "store", None)
                    current_before = str(getattr(store, "session_id", "") or "")
                    try:
                        resumed, message, history = _resume_chat_session(
                            session=built, target_session_id=str(target_session_id)
                        )
                    except Exception as exc:  # noqa: BLE001 - never crash the UI
                        _note(f"Resume failed: {exc}", role="error")
                        return
                    plain = _strip_rich_markup(message)
                    if not resumed:
                        _note(plain or "Resume failed.", role="error")
                        return
                    current_after = str(
                        getattr(getattr(built, "store", None), "session_id", "") or ""
                    )
                    if current_after == current_before:
                        # No-op resume (the chosen session is already active): do NOT
                        # wipe the transcript — just report it. (The picker excludes the
                        # current session, but a typed "/resume <current-id>" can land
                        # here via the direct-id fallback.)
                        _note(plain or "Session already active.", role="system")
                        return
                    # Real swap: reload the prior conversation + refresh HUD/footer.
                    loader = getattr(surface, "replace_history", None)
                    if callable(loader):
                        try:
                            loader(history)
                        except Exception:  # noqa: BLE001 - reload is best-effort
                            pass
                    # Restore the resumed session's persona (mode narrowing,
                    # write scope, model role) before the badge sync reads it.
                    try:
                        _reapply_resumed_persona(session=built, console=None)
                    except Exception:  # noqa: BLE001 - resume must still land
                        pass
                    try:
                        _tui_on_turn_complete()  # refresh tokens/cost/context
                    except Exception:
                        pass
                    _sync_tui_session_state(_tui_state, built, include_exec_mode=True)
                    new_model = str(getattr(getattr(built, "cfg", None), "model", "") or "")
                    if new_model:
                        _tui_state.model_name = new_model
                    # If the reloaded history had nothing visible (e.g. an interrupted
                    # session with only tool turns), the transcript is now empty and the
                    # welcome landing would hide a "system" line — use the assistant role
                    # so the confirmation flips the pane and is actually seen.
                    has_visible = any(
                        isinstance(m, dict)
                        and str(m.get("role") or "").strip().lower() in ("user", "assistant")
                        and isinstance(m.get("content"), str)
                        and m["content"].strip()
                        for m in (history or [])
                    )
                    _note(plain or "Resumed.", role="system" if has_visible else "assistant")

                def _tui_resume_select(value: str) -> None:
                    _tui_resume_apply(str(value))
                    return None

                def _tui_resume_picker() -> dict[str, Any] | None:
                    ctx = _tui_resume_context()
                    if ctx is None:
                        return None
                    _built, sessions_dir, current_session_id, ws_pick, git_pick = ctx
                    try:
                        candidates = _collect_chat_resume_candidates(
                            sessions_dir=sessions_dir,
                            current_session_id=current_session_id,
                            workspace_root=ws_pick,
                            git_root=git_pick,
                        )
                        spec = _chat_resume_picker_spec(sessions=candidates)
                    except Exception:  # noqa: BLE001 - never crash the UI on a picker
                        return None
                    if spec is None:
                        return None  # nothing to resume → runner prints guidance
                    spec["on_select"] = _tui_resume_select
                    return spec

                _tui_picker_providers = {
                    "/login": _tui_login_picker,
                    "/mode": _tui_mode_picker,
                    "/persona": _tui_persona_picker,
                    "/stream": _tui_stream_picker,
                    "/trace": _tui_trace_picker,
                    "/resume": _tui_resume_picker,
                    "/plan": _tui_forge_plan_picker,
                    "/assistant": _tui_forge_assistant_picker,
                    "/assets": _tui_assets_picker,
                }

                _tui_ok = False
                _tui_result: Any = None
                try:
                    _tui_result, _ = _run_tui(
                        _tui_state,
                        session_builder=None if subscription_blocked else _tui_session_builder,
                        on_turn_complete=None if subscription_blocked else _tui_on_turn_complete,
                        on_hud_refresh=None if subscription_blocked else _tui_on_turn_complete,
                        command_runner=_tui_command_runner,
                        panel_providers=_tui_panel_providers,
                        picker_providers=_tui_picker_providers,
                        persona_cycle=_tui_persona_cycle,
                        mode_cycle=_tui_mode_cycle,
                        completer=_tui_completer,
                        config_flow_factory=_tui_config_flow_factory,
                        on_config_saved=_tui_on_config_saved,
                        unavailable_message=(
                            subscription_availability.message if subscription_blocked else None
                        ),
                        subscription_provider_id=(
                            subscription_availability.provider_id
                            if subscription_blocked
                            and not subscription_availability.selection_required
                            else None
                        ),
                        open_config_on_start=bool(
                            subscription_blocked and subscription_availability.selection_required
                        ),
                    )
                    _tui_ok = True
                except Exception as _tui_exc:  # pragma: no cover - defensive fallback
                    console.print(
                        f"[yellow]TUI unavailable ({_tui_exc}); using classic chat.[/yellow]"
                    )
                finally:
                    _tui_session = _tui_box.get("session")
                    if _tui_session is not None:
                        try:
                            _tui_session.close()
                        except Exception:
                            pass
                if _tui_ok:
                    if (
                        isinstance(_tui_result, tuple)
                        and len(_tui_result) == 2
                        and _tui_result[0] == "login_connection"
                    ):
                        from ..commands.auth import login_connection_interactively
                        from ..commands.startup import _run_default_chat_action

                        login_connection_interactively(
                            str(_tui_result[1]),
                            console=console,
                        )
                        _run_default_chat_action(
                            path=Path(str(session_root)),
                            allow_broad_workspace=True,
                            mode=mode,
                            model=None,
                            base_url=None,
                            temperature=temperature,
                            stream=stream,
                            max_steps=max_steps,
                            subagents=subagents,
                            no_log=no_log,
                            verify_cmd=verify_cmd,
                            yes=yes,
                        )
                        return
                    # "Switch project" from /config exits with this sentinel; relaunch a
                    # fresh chat bound to the chosen folder (the old session is already
                    # closed above). We forward this session's execution-posture flags
                    # (mode/yes/no_log/verify_cmd/…) so switching project doesn't
                    # silently reset approval policy/mode/logging. NOTE: this re-enters
                    # chat() (one frame per switch); switching is a deliberate,
                    # heavyweight manual action so the depth is bounded in practice.
                    if (
                        isinstance(_tui_result, tuple)
                        and len(_tui_result) == 2
                        and _tui_result[0]
                        in {
                            "switch_workspace",
                            "restart_config",
                            "restart_routing_config",
                        }
                    ):
                        from ..commands.startup import _run_default_chat_action

                        connection_restart = _tui_result[0] == "restart_config"
                        _run_default_chat_action(
                            path=Path(str(_tui_result[1])),
                            allow_broad_workspace=True,
                            mode=mode,
                            model=None if connection_restart else model,
                            base_url=None if connection_restart else base_url,
                            temperature=temperature,
                            stream=stream,
                            max_steps=max_steps,
                            subagents=subagents,
                            no_log=no_log,
                            verify_cmd=verify_cmd,
                            yes=yes,
                        )
                    return
        if subscription_blocked:
            # The TUI failed after we intentionally deferred session construction.
            # Do not fall through and create the same guaranteed-to-fail session in
            # classic chat; preserve the actionable blocker instead.
            style = "red" if subscription_availability.is_error else "yellow"
            console.print(f"[{style}]{subscription_availability.message}[/{style}]")
            raise typer.Exit(code=1)
        printWelcome(
            console=console,
            workspace=focus_path if focus_path != session_root else session_root,
            model=str(effective.model or "?"),
        )
        console.print("[dim]Starting chat session...[/dim]")
        create_session_kwargs = {
            "cfg": effective,
            "root": session_root,
            "mode": effective_mode,
            "runtime_kind": RuntimeKind.INTERACTIVE_CHAT,
            "yes": yes,
            "max_steps": effective.max_steps,
            "no_log": no_log,
            "api_key_override": api_key_override,
            "console": console,
            "surface": _make_rich_surface(console=console, show_status_line=False),
            "non_interactive": non_interactive,
            "enable_chat_turn_step_budget": True,
            "chat_turn_fixed_override": (effective.max_steps if max_steps_provided else None),
            "verify_cmd": verify_cmd,
            "subagents_enabled": effective.subagents_enabled,
            "workspace_binding": workspace_binding,
            "crash_diagnostic_log_path": diagnostic_log,
        }
        try:
            session = create_session(**create_session_kwargs)
        except ConfigError as e:
            if not _is_default_shell_sandbox_startup_failure(cfg=effective, error=e):
                raise
            console.print(
                "[yellow]Shell sandbox unavailable:[/yellow] starting chat with shell execution disabled. "
                "Run `alysis doctor sandbox` for setup help, or set "
                "ALYSIS_SHELL_SANDBOX_MODE=off for explicit unsafe host execution."
            )
            create_session_kwargs["cfg"] = _cfg_with_warn_shell_sandbox_mode(effective)
            session = create_session(**create_session_kwargs)
        _set_chat_usage_hud_enabled(session, _resolve_usage_hud_default(effective))
        baseline_temperature = getattr(getattr(session, "client", None), "temperature", None)
        if baseline_temperature is None:
            baseline_temperature = getattr(effective, "chat_temperature", None)
        session._toolbar_default_temperature = baseline_temperature
        _refresh_chat_hud_context_cache(session)
        _ensure_session_summary_metadata(session=session, allow_model_summary=False)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(code=2) from e
    except WorkspaceBindingError as e:
        console.print(f"[red]Workspace error:[/red] {e}")
        raise typer.Exit(code=1) from e
    try:
        if not bool(getattr(session, "subagents_enabled", False)):
            console.print(
                "[dim]Tip: enable subagents with "
                "`alysis config set subagents_enabled true`, then start a new session.[/dim]"
            )
        pending_images = [os.fspath(p) for p in (image or [])]
        forge_state = _ForgeChatState()
        plan_mode_state = _ChatPlanModeState()
        _apply_startup_persona(session=session, console=console)
        prompt_session = _maybe_make_chat_prompt_session(
            console=console,
            root=focus_path,
            pending_images=pending_images,
            forge_state=forge_state,
            session=session,
            plan_mode_state=plan_mode_state,
        )
        if pending_images:
            console.print(f"Queued {len(pending_images)} image(s) for your next message.")
        while True:
            try:
                if prompt_session:
                    prompt_session_erases = bool(
                        getattr(prompt_session, "_alysis_erase_when_done", True)
                    )

                    def _bottom_toolbar(
                        _pending_images: list[str] = pending_images,
                    ) -> str:
                        return _chat_bottom_toolbar(
                            session=session,
                            pending_images=_pending_images,
                            forge_state=forge_state,
                            plan_mode_enabled=_chat_plan_mode_enabled(plan_mode_state),
                        )

                    prompt_result = prompt_session.prompt(
                        _chat_prompt_label_formatted(
                            ui_mode=forge_state.ui_mode,
                            mode=str(getattr(session, "mode", "")),
                        ),
                        bottom_toolbar=_bottom_toolbar,
                    )
                    if prompt_result is _CHAT_PROMPT_RESULT_PLAN_MODE_OFF:
                        user_msg = "/plan off"
                    else:
                        user_msg = prompt_result
                    if not prompt_session_erases and isinstance(user_msg, str):
                        _clear_submitted_prompt_line(
                            submitted_text=user_msg,
                            prompt_label=_chat_prompt_label(
                                ui_mode=forge_state.ui_mode,
                                mode=str(getattr(session, "mode", "")),
                            ),
                        )
                else:
                    fallback_label = _chat_prompt_fallback_label(
                        ui_mode=forge_state.ui_mode,
                        mode=str(getattr(session, "mode", "")),
                    )
                    user_msg = typer.prompt(fallback_label, prompt_suffix=" ")
                    _clear_submitted_prompt_line(
                        submitted_text=user_msg,
                        prompt_label=f"{fallback_label} ",
                    )
            except (EOFError, KeyboardInterrupt):
                console.print("")
                return

            command_result = _handle_chat_command(
                input_text=user_msg,
                root=focus_path,
                session=session,
                pending_images=pending_images,
                console=console,
                forge_state=forge_state,
                plan_mode_state=plan_mode_state,
                plan_mode_escape_supported=prompt_session is not None,
            )
            if command_result == "exit":
                return
            if command_result == "handled":
                continue
            if command_result == "send" and _chat_plan_mode_enabled(plan_mode_state):
                command_result = _resolve_interactive_plan_mode_request(
                    session=session,
                    console=console,
                    plan_mode_state=plan_mode_state,
                    user_message=user_msg,
                    plan_mode_escape_supported=prompt_session is not None,
                )
                if command_result == "handled":
                    continue

            execution_instruction = (
                command_result.instruction
                if isinstance(command_result, _ChatExecutionRequest)
                else user_msg
            )
            routing_mode_override = (
                command_result.routing_mode_override
                if isinstance(command_result, _ChatExecutionRequest)
                else None
            )
            ephemeral_system_messages = (
                list(command_result.ephemeral_system_messages)
                if isinstance(command_result, _ChatExecutionRequest)
                and command_result.ephemeral_system_messages
                else None
            )
            ephemeral_user_messages = (
                list(command_result.ephemeral_user_messages)
                if isinstance(command_result, _ChatExecutionRequest)
                and command_result.ephemeral_user_messages
                else None
            )
            temporary_mode_override = (
                command_result.mode_override
                if isinstance(command_result, _ChatExecutionRequest)
                else None
            )
            restore_mode_after = (
                command_result.restore_mode_after
                if isinstance(command_result, _ChatExecutionRequest)
                else None
            )
            plan_mode_capture_task = (
                command_result.plan_mode_capture_task
                if isinstance(command_result, _ChatExecutionRequest)
                else None
            )
            chat_only_turn = (
                bool(command_result.chat_only)
                if isinstance(command_result, _ChatExecutionRequest)
                else False
            )

            images_for_turn = pending_images.copy()
            interrupted = False
            llm_failed = False
            restored_mode_after_turn = False
            turn_start_messages = (
                len(getattr(session, "messages", []) or [])
                if plan_mode_capture_task is not None
                else 0
            )
            if temporary_mode_override is not None:
                try:
                    _apply_chat_effective_mode(
                        session=session,
                        next_mode=temporary_mode_override,
                        persist_default_mode=False,
                    )
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]Failed to prepare approved execution:[/red] {e}")
                    continue
            try:
                with _chat_turn_interrupt_monitor():
                    run_turn_kwargs: dict[str, Any] = {"image_paths": images_for_turn or None}
                    if routing_mode_override is not None:
                        run_turn_kwargs["routing_mode_override"] = routing_mode_override
                    combined_ephemeral_system = list(ephemeral_system_messages or [])
                    combined_ephemeral_system += persona_overlay_messages(
                        cfg=getattr(session, "cfg", None),
                        persona=getattr(session, "persona", "code"),
                        registry=_session_persona_registry(session),
                    )
                    if combined_ephemeral_system:
                        run_turn_kwargs["ephemeral_system_messages"] = combined_ephemeral_system
                    combined_ephemeral_user = persona_overlay_user_messages(
                        cfg=getattr(session, "cfg", None),
                        persona=getattr(session, "persona", "code"),
                        registry=_session_persona_registry(session),
                    )
                    combined_ephemeral_user += list(ephemeral_user_messages or [])
                    if combined_ephemeral_user:
                        run_turn_kwargs["ephemeral_user_messages"] = combined_ephemeral_user
                    if chat_only_turn:
                        run_turn_kwargs["chat_only"] = True
                    session.run_turn(execution_instruction, **run_turn_kwargs)
            except KeyboardInterrupt:
                interrupted = True
                _finish_chat_surface_activity(session=session)
                console.print(
                    "[yellow]Interrupted current turn.[/yellow] "
                    "You can send a new message or use /exit."
                )
            except LLMError as e:
                llm_failed = True
                _render_chat_llm_error(session=session, console=console, error=e)
            finally:
                if restore_mode_after is not None:
                    try:
                        _apply_chat_effective_mode(
                            session=session,
                            next_mode=restore_mode_after,
                            persist_default_mode=False,
                        )
                        restored_mode_after_turn = True
                    except Exception as e:  # noqa: BLE001
                        console.print(
                            f"[red]Failed to restore Plan Mode after execution:[/red] {e}"
                        )
                # An approved switch_mode proposal applies after mode-override
                # restoration so the persona's clamp works from the user's
                # real base mode, never a temporary /ask override.
                _apply_pending_persona_switch(session=session, console=console)

            if llm_failed:
                # Keep queued images so the user can retry without re-attaching.
                pending_images = images_for_turn
                _refresh_chat_hud_context_cache(session)
                continue

            pending_images.clear()
            if restored_mode_after_turn:
                _refresh_chat_hud_context_cache(session)
            _refresh_chat_hud_context_cache(session)
            if interrupted:
                continue
            if plan_mode_capture_task is not None:
                _record_plan_mode_draft_from_turn(
                    session=session,
                    console=console,
                    plan_mode_state=plan_mode_state,
                    user_message=plan_mode_capture_task,
                    start_index=turn_start_messages,
                )
            _ensure_session_summary_metadata(session=session, allow_model_summary=False)
            usage_result = _chat_turn_usage_line(session)
            if usage_result is not None:
                usage_line, usage_warning_line = usage_result
                if usage_line:
                    console.print(usage_line, style="dim", highlight=False)
                if usage_warning_line:
                    console.print(
                        usage_warning_line,
                        style=_chat_turn_usage_style(session),
                        highlight=False,
                    )
    finally:
        session.close()


def run(
    instruction: str = typer.Argument(..., help="What you want the agent to do."),
    path: Path = typer.Option(Path("."), "--path", help="Working directory/root."),
    create_path: bool = typer.Option(
        False,
        "--create-path",
        help="Create --path if it does not exist before binding the workspace.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces in non-interactive startup flows.",
    ),
    image: list[Path] | None = typer.Option(
        None,
        "--image",
        help="Attach image path(s). Repeat --image for multiple files.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    persona: str | None = typer.Option(
        None,
        "--persona",
        help=(
            "Persona for this one-shot run: code, architect, ask, debug, or a "
            "custom persona from .alysis_personas."
        ),
    ),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on agent iterations.",
    ),
    subagents: bool | None = typer.Option(
        None,
        "--subagents/--no-subagents",
        help="Enable or disable subagent delegation for this session.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verification command for this run (repeatable).",
    ),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    benchmark: bool = typer.Option(
        False,
        "--benchmark",
        help=(
            "Use the raw benchmark/autonomy run profile: auto mode, code-only routing, "
            "longer fixed step budget, and no subagents/skills/custom tools/web by default."
        ),
    ),
    deadline_seconds: float | None = typer.Option(
        None,
        "--deadline-seconds",
        help=(
            "Stop this one-shot run after the given invocation-wide wall-clock seconds. "
            "Defaults to 3600 (60 min); use --no-deadline to run unbounded."
        ),
    ),
    no_deadline: bool = typer.Option(
        False,
        "--no-deadline",
        help="Run without any wall-clock budget, overriding the default one-shot deadline.",
    ),
    require_deadline: bool = typer.Option(
        False,
        "--require-deadline",
        help=(
            "Require a finite one-shot run deadline from CLI, environment, or config. "
            "Intended for managed hosts."
        ),
    ),
    diagnostic_log: Path | None = typer.Option(
        None,
        "--diagnostic-log",
        help="Append minimal crash-safe diagnostic events to this JSONL path.",
    ),
    cli_ctx: Any = None,
) -> None:
    console = _console()
    cfg = load_config()
    current_ctx = cli_ctx if cli_ctx is not None else get_current_context(silent=True)
    non_interactive = _is_non_interactive_terminal()
    path_source = current_ctx.get_parameter_source("path") if current_ctx is not None else None
    max_steps_source = (
        current_ctx.get_parameter_source("max_steps") if current_ctx is not None else None
    )
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        max_steps_provided = (
            max_steps is not None
            and max_steps_source is not None
            and max_steps_source is not ParameterSource.DEFAULT
        )

    effective = clone_cfg(cfg)
    raw_benchmark_profile = _raw_benchmark_profile_requested(benchmark=benchmark)
    if raw_benchmark_profile:
        _apply_raw_benchmark_profile(effective)
    if base_url is not None:
        effective.base_url = base_url
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    if stream is not None:
        effective.stream = stream
    else:
        # Interactive chat is more readable with incremental output by default.
        effective.stream = True
    if max_steps is not None:
        effective.max_steps = max_steps
    if subagents is not None:
        effective.subagents_enabled = subagents

    effective_mode = (mode.value if mode else effective.default_mode) or "review"
    effective_yes = bool(yes or raw_benchmark_profile)
    fixed_step_override = (
        effective.max_steps if (max_steps_provided or raw_benchmark_profile) else None
    )
    delegated_execution = effective.execution.backend == "delegated"
    if delegated_execution and mode is None and str(effective_mode).strip().lower() == "fullaccess":
        effective_mode = "review"
        console.print(
            "[yellow]Delegated runtimes do not inherit the native fullaccess default; "
            "using review (read-only). Pass --mode auto to allow workspace writes.[/yellow]"
        )
    binding_source = _path_binding_source(path_source, path)

    try:
        if not delegated_execution and not effective.model:
            raise ConfigError("Model is not set. Run: alysis config set model <MODEL>")
        api_key_override = None
        if delegated_execution:
            from ...agent_runtimes.host import (
                prepare_delegated_runtime,
                run_delegated_once,
                validate_delegated_cli_options,
            )

            validate_delegated_cli_options(
                base_url=base_url,
                temperature=temperature,
                max_steps=max_steps,
                subagents=subagents,
                verify_cmd=verify_cmd,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
                api_key=api_key,
                stream=stream,
                yes=yes,
                benchmark=raw_benchmark_profile,
                diagnostic_log=diagnostic_log,
            )
        else:
            api_key_override = _resolve_api_key_override(
                api_key=api_key,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
            )
        workspace_binding = _resolve_startup_workspace_binding(
            requested_path=path,
            console=console,
            interactive=not non_interactive,
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            source=binding_source,
            action=WorkspaceAction.CHAT,
        )
        run_persona_overlays: list[str] | None = None
        run_persona_user_overlays: list[str] | None = None
        run_persona_globs: list[str] | None = None
        if persona is not None:
            from ...config import resolve_role_temperature
            from ...model_router import resolve_model_for_role
            from ...personas import (
                load_custom_personas,
            )
            from ...personas import (
                persona_overlay_messages as _persona_overlays_fn,
            )
            from ...personas import (
                persona_overlay_user_messages as _persona_user_overlays_fn,
            )
            from ...personas import (
                resolve_persona_model_role as _resolve_persona_role,
            )

            normalized_run_persona = str(persona).strip().lower()
            if delegated_execution:
                console.print("[red]--persona is not supported with delegated execution.[/red]")
                raise typer.Exit(code=2)
            if not persona_modes_enabled(effective):
                console.print("[red]Persona modes are disabled.[/red]")
                raise typer.Exit(code=2)
            run_registry, run_persona_warnings = load_custom_personas(
                workspace_binding.workspace_context.workspace_root
            )
            for warning in run_persona_warnings:
                console.print(f"[yellow]Custom persona:[/yellow] {warning}")
            if not is_persona_name(normalized_run_persona, run_registry):
                console.print(f"[red]Unknown persona:[/red] {normalized_run_persona}")
                raise typer.Exit(code=2)
            run_definition = get_persona(normalized_run_persona, run_registry)
            # Same clamp as interactive switching: the persona may lower the
            # requested mode, never raise it.
            effective_mode = resolve_persona_exec_mode(run_definition, effective_mode)
            if run_definition.allow_write_globs:
                run_persona_globs = list(run_definition.allow_write_globs)
            run_persona_overlays = (
                _persona_overlays_fn(
                    cfg=effective, persona=normalized_run_persona, registry=run_registry
                )
                or None
            )
            run_persona_user_overlays = (
                _persona_user_overlays_fn(
                    cfg=effective, persona=normalized_run_persona, registry=run_registry
                )
                or None
            )
            if model is None:
                persona_role = _resolve_persona_role(
                    effective, normalized_run_persona, run_registry
                )
                resolved_persona_model = str(
                    resolve_model_for_role(cfg=effective, role=persona_role, plan=None) or ""
                ).strip()
                if resolved_persona_model and resolved_persona_model != effective.model:
                    effective.model = resolved_persona_model
            else:
                persona_role = _resolve_persona_role(
                    effective, normalized_run_persona, run_registry
                )
            # create_session constructs the one-shot main client through the
            # coding slot. Carry the selected persona role's temperature into
            # that slot so explicit zero values and per-role tuning survive.
            effective.coding_temperature = resolve_role_temperature(
                effective,
                role=persona_role,
            )
            effective.default_persona = normalized_run_persona
        if delegated_execution:
            from ...config import resolve_run_deadline
            from ...execution_deadline import DEFAULT_RUN_DEADLINE_SECONDS

            delegated_default_deadline = (
                None if (require_deadline or no_deadline) else DEFAULT_RUN_DEADLINE_SECONDS
            )
            resolved_deadline = resolve_run_deadline(
                effective,
                cli_deadline_seconds=deadline_seconds,
                cli_no_deadline=no_deadline,
                default_seconds=delegated_default_deadline,
            )
            delegated_deadline = resolved_deadline.seconds
            if require_deadline and delegated_deadline is None:
                raise ConfigError(
                    "Managed-host run requires a finite run deadline. Pass --deadline-seconds, "
                    "set ALYSIS_RUN_DEADLINE_SECONDS, or configure run_deadline_seconds."
                )
            prepare_delegated_runtime(
                effective,
                model=model,
                deadline_seconds=delegated_deadline,
            )
            code = run_delegated_once(
                cfg=effective,
                cwd=workspace_binding.workspace_context.workspace_root,
                instruction=instruction,
                mode=effective_mode,
                image_paths=tuple(path.resolve() for path in (image or ())),
                no_log=no_log,
                console=console,
            )
        else:
            code = run_agent(
                cfg=effective,
                root=workspace_binding.workspace_context.workspace_root,
                instruction=instruction,
                image_paths=[os.fspath(p) for p in (image or [])],
                mode=effective_mode,
                persona_allow_write_globs=run_persona_globs,
                ephemeral_system_messages=run_persona_overlays,
                ephemeral_user_messages=run_persona_user_overlays,
                runtime_kind=RuntimeKind.ONE_SHOT,
                yes=effective_yes,
                max_steps=effective.max_steps,
                no_log=no_log,
                api_key_override=api_key_override,
                console=console,
                surface=_make_rich_surface(console=console),
                non_interactive=non_interactive,
                usage_role="run",
                subagents_enabled=effective.subagents_enabled,
                one_shot_execution=True,
                enable_chat_turn_step_budget=True,
                chat_turn_fixed_override=fixed_step_override,
                enable_tool_output_offload=(True if raw_benchmark_profile else None),
                compaction_profile="execution",
                verify_cmd=verify_cmd,
                workspace_binding=workspace_binding,
                run_deadline_seconds=deadline_seconds,
                no_run_deadline=no_deadline,
                require_run_deadline=require_deadline,
                crash_diagnostic_log_path=diagnostic_log,
            )
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(code=2) from e
    except WorkspaceBindingError as e:
        console.print(f"[red]Workspace error:[/red] {e}")
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001
        # Prefer friendly Alysis Code MiMo trial copy (trial_expired, rate-limit, ...)
        # over a raw ``LLM error 402: {...}`` dump; any other error renders as-is.
        message = sanitize_error_text_for_output(e)
        try:
            from ...llm.openai_compat import alysis_trial_error_message

            message = sanitize_error_text_for_output(alysis_trial_error_message(e) or message)
        except Exception:  # noqa: BLE001
            pass
        exit_code = exit_code_for_failure(e)
        label = (
            "Infrastructure error after retries"
            if exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE
            else "Error"
        )
        try:
            console.print(f"[red]{label}:[/red] {message}")
        except Exception as render_exc:  # noqa: BLE001 - CLI error rendering must not double-crash.
            safe_plain_error(
                stream=getattr(console, "file", None),
                error_type=type(render_exc).__name__,
                message=sanitize_error_text_for_output(e),
            )
        raise typer.Exit(code=exit_code) from e

    raise typer.Exit(code=code)


def _handle_chat_command_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _handle_chat_command(*args, **kwargs)


def _handle_forge_chat_command_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _handle_forge_chat_command(*args, **kwargs)


def _run_plan_mode_approval_loop_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _run_plan_mode_approval_loop(*args, **kwargs)


def _print_chat_context_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _print_chat_context(*args, **kwargs)


def chat_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return chat(*args, **kwargs)


def run_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return run(*args, **kwargs)
