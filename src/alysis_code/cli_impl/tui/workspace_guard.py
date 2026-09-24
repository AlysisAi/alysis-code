"""TUI-native pickers for the guarded-workspace startup prompt.

When ``ALYSIS_TUI`` is on, the startup workspace-guard resolution (broad/risky
workspace → choose an action, then pick a project / type a path) is rendered with
the same visual grammar as the chat + setup TUIs — the green ``#3fb950`` accent,
the dark picker panel, and the shared :func:`_render_picker_rows` renderer —
instead of the classic Rich/inline prompt.

These functions are drop-in replacements for the ``select_action_interactive`` /
``select_candidate_interactive`` / ``prompt_text`` callbacks that
:func:`alysis_code.workspace_binding_ui.resolve_startup_workspace_binding`
already accepts, so the binding *logic* is untouched: we only change how each step
is painted. Each step is its own short-lived full-screen ``Application`` (the
binding resolver drives the loop), mirroring how the classic selectors run one
inline picker per call.

Every entry point degrades gracefully: if a real terminal is unavailable (or
prompt_toolkit raises), the action/candidate selectors return
``(None, False)`` so the resolver falls back to its typed-prompt path, exactly
like the classic selectors do.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .app import _ACCENT, _render_picker_rows, _wrap_line
from .app import _STYLE as _CHAT_STYLE
from .selection_bindings import selection_editing_bindings

# The shared row builders prefix labels with "N) " for the classic numbered
# prompt; the TUI picker draws its own "N." gutter, so strip the leading number.
_NUM_PREFIX = re.compile(r"^\s*\d+\)\s*")

_PICKER_HINT = "↑/↓ move · Enter confirm · 1-9 quick-pick · Esc cancel"
_INPUT_HINT = "Enter confirm · Esc cancel"

# ---- startup input stash ---------------------------------------------------
# Input that arrives before a guard picker has ever been painted was typed at a
# blank screen: it is the user's *message*, not a picker command. Pre-render
# keystrokes therefore never act on the picker (a buffered digit used to
# quick-pick - and on the candidate step, bind a workspace - sight-unseen)
# and printable ones are stashed here instead of dropped. The chat loop
# drains the stash into the chat input once the TUI starts.
_PREFILL_STASH: list[str] = []


def _stash_enabled() -> bool:
    """Kill switch ``ALYSIS_STARTUP_INPUT_STASH`` (default on).

    Off restores the previous behavior: buffered input acts on the pickers
    immediately and unbound keys are dropped.
    """
    value = os.environ.get("ALYSIS_STARTUP_INPUT_STASH", "").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _stash_gating_active(input: Any) -> bool:
    """Whether the pre-render input gate applies to this picker run.

    The blank-screen race is a real-terminal phenomenon: the kernel buffers
    what the user typed before the picker attached. Injected test inputs
    (``input is not None``) are exempt so the existing headless tests keep
    driving pickers with preloaded keys; set
    ``ALYSIS_STARTUP_INPUT_STASH_FORCE=1`` to exercise the gate with an
    injected input (the stash tests do).
    """
    if not _stash_enabled():
        return False
    if input is None:
        return True
    force = os.environ.get("ALYSIS_STARTUP_INPUT_STASH_FORCE", "").strip().lower()
    return force in {"1", "true", "on", "yes"}


def _stash_printable(data: Any) -> None:
    if not _stash_enabled():
        return
    text = str(data or "")
    if text and text.isprintable():
        _PREFILL_STASH.append(text)


# How long after a guard app starts running that keys still count as "typed at
# the blank screen". Buffered bytes are delivered by prompt_toolkit within
# milliseconds of run start (the kernel had them queued before the app even
# existed); a human reacting to a freshly painted picker needs ≥ ~300 ms. A
# key inside the window is stashed instead of acted on; the failure mode of a
# grotesquely slow machine is the old pre-fix behavior, never a new one.
_STASH_GRACE_SECONDS = 0.35


def drain_startup_input_stash() -> str:
    """Return and clear text typed at unpainted startup pickers."""
    text = "".join(_PREFILL_STASH)
    _PREFILL_STASH.clear()
    return text


def _strip_num(label: str) -> str:
    return _NUM_PREFIX.sub("", str(label)).strip()


def _guard_style() -> Any:
    from prompt_toolkit.styles import Style, merge_styles

    extra = Style.from_dict(
        {
            "wsg.header": f"bold {_ACCENT}",
            "wsg.headerdim": "#6e7681",
            "wsg.title": "bold #e6edf3",
            "wsg.subtitle": "#8b949e",
            "wsg.footer": "#6e7681",
            "wsg.label": "#c9d1d9",
            "wsg.dim": "#6e7681",
        }
    )
    return merge_styles([_CHAT_STYLE, extra])


def _header_fragments(subtitle: str) -> Any:
    from prompt_toolkit.formatted_text import FormattedText

    return FormattedText(
        [
            ("class:wsg.header", "◆ alysis"),
            ("class:wsg.headerdim", f"  ·  {subtitle}"),
        ]
    )


def _footer_fragments(hint: str) -> Any:
    from prompt_toolkit.formatted_text import FormattedText

    return FormattedText([("class:wsg.footer", hint)])


def _run_option_picker(
    *,
    subtitle: str,
    title: str,
    rows: list[tuple[str, str, str]],
    default_index: int = 0,
    input: Any | None = None,
    output: Any | None = None,
) -> tuple[str | None, bool]:
    """Render a selectable option list and return ``(value, interactive_available)``.

    ``rows`` are ``(value, label, description)`` tuples (the label may carry the
    classic ``N)`` prefix, which is stripped). ``value`` of the chosen row is
    returned, or ``None`` when the user cancels (Esc / Ctrl-C). On any terminal /
    prompt_toolkit failure the call returns ``(None, False)`` so the resolver can
    fall back to its typed prompt.
    """
    if not rows:
        return None, True
    try:
        from prompt_toolkit.application import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, ScrollOffsets, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.margins import ScrollbarMargin
    except Exception:
        return None, False

    picker_rows = [
        {"label": _strip_num(label), "description": desc, "value": value}
        for value, label, desc in rows
    ]
    state: dict[str, Any] = {
        "index": max(0, min(default_index, len(picker_rows) - 1)),
        "result": None,
    }
    cursor = {"row": 0}

    def _width(win: Window, fallback: int = 80) -> int:
        info = win.render_info
        if info is not None and info.window_width:
            return int(info.window_width)
        try:
            return max(20, get_app().output.get_size().columns - 2)
        except Exception:
            return fallback

    def _body() -> FormattedText:
        width = _width(body_window)
        out_rows: list[list[tuple[str, str]]] = []
        for line in _wrap_line(title, width):
            out_rows.append([("class:wsg.title", line)])
        out_rows.append([("", "")])  # spacer
        header_lines = len(out_rows)
        # Hint lives in the pinned footer only (it stays put while a long list
        # scrolls), so suppress the picker's own trailing hint to avoid a dupe.
        picker = _render_picker_rows(picker_rows, state["index"], width, hint="")
        located = next(
            (i for i, prow in enumerate(picker) if any("selcaret" in s for s, _ in prow)),
            0,
        )
        cursor["row"] = header_lines + located
        out_rows.extend(picker)
        flat: list[tuple[str, str]] = []
        for row in out_rows:
            flat.extend(row)
            flat.append(("", "\n"))
        return FormattedText(flat)

    body_window = Window(
        FormattedTextControl(
            _body,
            focusable=True,
            get_cursor_position=lambda: Point(0, cursor["row"]),
        ),
        wrap_lines=True,
        scroll_offsets=ScrollOffsets(top=1, bottom=1),
        right_margins=[ScrollbarMargin(display_arrows=False)],
    )
    root = HSplit(
        [
            Window(FormattedTextControl(lambda: _header_fragments(subtitle)), height=1),
            Window(height=1),
            body_window,
            Window(height=1),
            Window(FormattedTextControl(lambda: _footer_fragments(_PICKER_HINT)), height=1),
        ]
    )

    kb = KeyBindings()

    # Blank-screen stash gate: keys processed within the grace window of this
    # picker starting to run were typed at a blank screen (the kernel had them
    # buffered before the picker existed - prompt_toolkit delivers them right
    # after its first synchronous redraw). They must not navigate, quick-pick,
    # confirm or (Esc) cancel a UI nobody has seen; printable ones are kept
    # for the chat input instead. Ctrl-C / Ctrl-D stay live as the abort.
    started = {"t": None}
    stash_active = _stash_gating_active(input)

    def _gate_open() -> bool:
        if not stash_active:
            return True
        t0 = started["t"]
        return t0 is not None and (time.monotonic() - t0) > _STASH_GRACE_SECONDS

    live = Condition(_gate_open)

    def _invalidate() -> None:
        try:
            get_app().invalidate()
        except Exception:
            pass

    @kb.add("up", eager=True, filter=live)
    @kb.add("k", eager=True, filter=live)
    def _up(event: Any) -> None:
        state["index"] = (state["index"] - 1) % len(picker_rows)
        _invalidate()

    @kb.add("down", eager=True, filter=live)
    @kb.add("j", eager=True, filter=live)
    def _down(event: Any) -> None:
        state["index"] = (state["index"] + 1) % len(picker_rows)
        _invalidate()

    @kb.add("enter", eager=True, filter=live)
    def _confirm(event: Any) -> None:
        state["result"] = picker_rows[state["index"]]["value"]
        event.app.exit()

    for _digit in "123456789":

        @kb.add(_digit, eager=True, filter=live)
        def _pick(event: Any) -> None:
            try:
                idx = int(event.data) - 1
            except (TypeError, ValueError):
                return
            if 0 <= idx < len(picker_rows):
                state["index"] = idx
                state["result"] = picker_rows[idx]["value"]
                event.app.exit()

    @kb.add("escape", filter=live)
    @kb.add("c-c")
    @kb.add("c-d")
    def _cancel(event: Any) -> None:
        state["result"] = None
        event.app.exit()

    # NOT eager: an eager wildcard would fire ahead of the exact Ctrl-C /
    # Ctrl-D bindings and swallow the always-on abort. As a plain wildcard it
    # only receives keys no active exact binding claims.
    @kb.add(Keys.Any, filter=~live)
    def _stash_in_grace(event: Any) -> None:
        _stash_printable(getattr(event, "data", None))

    def _mark_started() -> None:
        started["t"] = time.monotonic()

    app: Application = Application(
        layout=Layout(root, focused_element=body_window),
        key_bindings=kb,
        style=_guard_style(),
        full_screen=True,
        mouse_support=False,
        input=input,
        output=output,
    )
    try:
        app.run(pre_run=_mark_started)
    except Exception:
        return None, False
    return state["result"], True


def select_guarded_workspace_action(
    *,
    binding: Any,
    candidates: tuple[Any, ...],
    allow_use_current_action: bool = True,
    console: Any | None = None,
    input: Any | None = None,
    output: Any | None = None,
) -> tuple[str | None, bool]:
    """TUI replacement for the guarded-workspace action selector."""
    from ...workspace_binding_ui import guarded_workspace_action_rows

    rows = guarded_workspace_action_rows(
        binding=binding,
        candidates=candidates,
        allow_use_current_action=allow_use_current_action,
    )
    # Default to "choose project" when offered (index 1 once "use current" leads),
    # else the first row — same default the classic selector uses.
    default_index = 1 if allow_use_current_action and len(rows) > 1 else 0
    path_label = os.fspath(getattr(binding, "requested_path", ""))
    return _run_option_picker(
        subtitle="workspace",
        title=f"Guarded workspace: {path_label}",
        rows=rows,
        default_index=default_index,
        input=input,
        output=output,
    )


def select_workspace_candidate(
    *,
    base_path: Path,
    candidates: tuple[Any, ...],
    console: Any | None = None,
    input: Any | None = None,
    output: Any | None = None,
) -> tuple[Path | None, bool]:
    """TUI replacement for the project-folder candidate selector."""
    from ...workspace_binding_ui import workspace_candidate_rows

    rows = workspace_candidate_rows(base_path=base_path, candidates=candidates)
    if not rows:
        return None, True
    value, interactive_available = _run_option_picker(
        subtitle="workspace",
        title=f"Project folders under {os.fspath(base_path)}",
        rows=rows,
        default_index=0,
        input=input,
        output=output,
    )
    if value is None:
        return None, interactive_available
    return Path(value), interactive_available


def workspace_guard_prompt_text(
    text: str,
    *,
    default: str | None = None,
    input: Any | None = None,
    output: Any | None = None,
) -> str:
    """TUI replacement for the guarded-workspace typed prompt.

    Returns the entered text (or ``default`` when blank). Raises
    ``KeyboardInterrupt`` on Esc / Ctrl-C so the resolver treats it as "go back"
    — matching the classic ``_prompt_text_with_escape`` contract.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.processors import BeforeInput
    from prompt_toolkit.widgets import Frame, TextArea

    input_area = TextArea(
        height=1,
        multiline=False,
        wrap_lines=False,
        style="class:tui.input",
        input_processors=[BeforeInput("> ", style="class:tui.prompt")],
    )

    def _label() -> FormattedText:
        frags: list[tuple[str, str]] = [("class:wsg.label", f"{text}")]
        return FormattedText(frags)

    def _hint() -> FormattedText:
        if default is not None:
            return FormattedText([("class:wsg.dim", f"default: {default}  (Enter to accept)")])
        return FormattedText([("", "")])

    has_default = default is not None
    root = HSplit(
        [
            Window(FormattedTextControl(lambda: _header_fragments("workspace")), height=1),
            Window(height=1),
            Window(FormattedTextControl(_label), height=1),
            ConditionalContainer(
                Window(FormattedTextControl(_hint), height=1),
                filter=Condition(lambda: has_default),
            ),
            Frame(input_area),
            Window(height=1),
            Window(FormattedTextControl(lambda: _footer_fragments(_INPUT_HINT)), height=1),
        ]
    )

    kb = KeyBindings()
    result: dict[str, Any] = {"text": None, "cancelled": False}

    # Blank-screen stash gate (see _run_option_picker): a buffered Enter must not
    # accept the default and a buffered Esc must not cancel before anyone saw
    # this prompt. Typed characters are fine here - they land in the visible
    # text field, where the user can see and edit them.
    started = {"t": None}
    stash_active = _stash_gating_active(input)

    def _gate_open() -> bool:
        if not stash_active:
            return True
        t0 = started["t"]
        return t0 is not None and (time.monotonic() - t0) > _STASH_GRACE_SECONDS

    live = Condition(_gate_open)

    @kb.add("enter", eager=True, filter=live)
    def _submit(event: Any) -> None:
        result["text"] = input_area.text
        event.app.exit()

    @kb.add("escape", filter=live)
    @kb.add("c-c")
    @kb.add("c-d")
    def _cancel(event: Any) -> None:
        result["cancelled"] = True
        event.app.exit()

    def _mark_started() -> None:
        started["t"] = time.monotonic()

    app: Application = Application(
        layout=Layout(root, focused_element=input_area),
        key_bindings=merge_key_bindings([selection_editing_bindings(), kb]),
        style=_guard_style(),
        full_screen=True,
        mouse_support=False,
        input=input,
        output=output,
    )
    app.run(pre_run=_mark_started)
    if result["cancelled"]:
        raise KeyboardInterrupt
    entered = str(result["text"] or "").strip()
    if not entered and default is not None:
        return default
    return entered


__all__ = [
    "drain_startup_input_stash",
    "select_guarded_workspace_action",
    "select_workspace_candidate",
    "workspace_guard_prompt_text",
]
