"""Full-screen TUI front-end for the first-run setup wizard.

A prompt_toolkit ``Application`` that walks a :class:`SetupFlow` through the
onboarding steps using the same visual grammar as the chat TUI: the white owl on
the welcome/finish screens, the green ``#3fb950`` accent, the reusable picker
renderer, and a pinned footer. The flow owns all of the wizard *logic*; this
module only paints its :class:`Screen` and routes key events back into it.

Focus is held permanently on a single hidden ``TextArea`` (shown only on input
steps). Navigation in list/confirm/message screens runs through eager global key
bindings — exactly how the chat TUI's picker works — so we never juggle focus
across the worker thread that runs the network/validation steps.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.application.current import get_app
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, WindowAlign
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, ScrollOffsets, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import (
    BeforeInput,
    ConditionalProcessor,
    PasswordProcessor,
)
from prompt_toolkit.styles import Style, merge_styles
from prompt_toolkit.widgets import Frame, TextArea

from .app import _ACCENT, _SPINNER_FRAMES, _render_picker_rows, _wrap_line
from .app import _STYLE as _CHAT_STYLE
from .owl import load_owl_animation
from .setup_flow import Row, SetupFlow

# Tones map to concrete styles. Green is the only accent — degraded/neutral
# states stay plain or amber so the accent reads clearly (same discipline as the
# chat TUI's panels).
_TONE_STYLE = {
    "ok": "class:setup.ok",
    "warn": "class:setup.warn",
    "err": "class:setup.err",
    "dim": "class:setup.dim",
    "plain": "class:setup.text",
}

_SETUP_STYLE = Style.from_dict(
    {
        "setup.header": f"bold {_ACCENT}",
        "setup.headerdim": "#6e7681",
        "setup.title": f"bold {_ACCENT}",
        "setup.subtitle": "#8b949e",
        "setup.text": "#c9d1d9",
        "setup.dim": "#6e7681",
        # Prominent call-to-action (e.g. "Press Enter to begin") on message screens.
        "setup.cta": f"bold {_ACCENT}",
        "setup.ok": f"bold {_ACCENT}",
        "setup.warn": "#d19a66",
        "setup.err": "#e06c75",
        "setup.spinner": f"bold {_ACCENT}",
        "setup.footer.brand": f"bold {_ACCENT}",
        "setup.footer.dim": "#6e7681",
        "setup.footer.progress": "#8b949e",
    }
)

_STYLE = merge_styles([_CHAT_STYLE, _SETUP_STYLE])


def _row_to_dict(row: Row) -> dict[str, Any]:
    return {
        "label": row.label,
        "description": row.description,
        "value": row.value,
        "current": row.current,
        "kind": row.kind,
        "tone": row.tone,
    }


def run_setup_tui(
    *,
    owl_color: bool = True,
    input: Any | None = None,
    output: Any | None = None,
    inline_busy: bool = False,
) -> bool:
    """Run the alt-screen setup wizard.

    Returns ``True`` when setup was saved, ``False`` when the user cancelled.
    Raises on infrastructure failure (no usable terminal, prompt_toolkit error)
    so the caller can fall back to the classic Rich wizard.

    ``inline_busy`` runs the network/validation steps synchronously inside the
    key handler instead of on a worker thread; tests use it so a pre-loaded key
    sequence drives the whole flow deterministically.
    """
    flow = SetupFlow()
    owl = load_owl_animation(color_enabled=owl_color)

    spinner = {"i": 0}
    busy = {"running": False}
    report = {"text": ""}
    # Cached cursor row for the scrollable body so the selected list option stays
    # in view (the chat TUI's cursor-pin trick, mirrored here).
    body_cursor = {"row": 0}
    # Tracks the last stage the input box was primed for, so we reset/seed the
    # buffer exactly once per stage change (never mid-edit).
    last_stage: dict[str, Any] = {"v": None}

    def _invalidate() -> None:
        try:
            get_app().invalidate()
        except Exception:
            pass

    flow.set_report(lambda msg: (report.__setitem__("text", str(msg)), _invalidate()))

    # ----------------------------------------------------------- rendering
    def _width(win: Window, fallback: int = 80) -> int:
        info = win.render_info
        if info is not None and info.window_width:
            return int(info.window_width)
        try:
            return max(20, get_app().output.get_size().columns - 2)
        except Exception:
            return fallback

    def _header_text() -> FormattedText:
        return FormattedText(
            [
                ("class:setup.header", "◇ alysis"),
                ("class:setup.headerdim", "  ·  setup"),
            ]
        )

    def _message_fragments() -> FormattedText:
        scr = flow.screen()
        frags: list[tuple[str, str]] = [("", "\n")]
        if scr.stage == "welcome":
            owl_ansi = owl.current_ansi()
            if owl_ansi is not None:
                frags.extend(to_formatted_text(owl_ansi))
                frags.append(("", "\n\n"))
        if scr.title:
            frags.append(("class:setup.title", scr.title))
            frags.append(("", "\n"))
        if scr.subtitle:
            frags.append(("class:setup.subtitle", scr.subtitle))
            frags.append(("", "\n"))
        frags.append(("", "\n"))
        for text, tone in scr.lines:
            frags.append((_TONE_STYLE.get(tone, "class:setup.text"), text))
            frags.append(("", "\n"))
        if scr.hint:
            # The call-to-action stands out in bold green with a blank line above.
            frags.append(("", "\n\n"))
            frags.append(("class:setup.cta", scr.hint))
            frags.append(("", "\n"))
        return FormattedText(frags)

    def _body_fragments() -> FormattedText:
        scr = flow.screen()
        width = _width(body_window)
        rows: list[list[tuple[str, str]]] = []
        # Title + subtitle header. Both are pre-wrapped to the content width so
        # header_lines equals the true screen-row count — the cursor-pin scroll
        # math (which counts logical rows) requires every emitted row <= width.
        if scr.title:
            for line in _wrap_line(scr.title, width):
                rows.append([("class:setup.title", line)])
        if scr.subtitle:
            for line in _wrap_line(scr.subtitle, width):
                rows.append([("class:setup.subtitle", line)])
        rows.append([("", "")])  # blank spacer
        header_lines = len(rows)
        sel_line = header_lines

        if scr.mode == "list" and scr.rows:
            picker = _render_picker_rows(
                [_row_to_dict(r) for r in scr.rows], scr.index, width, hint=scr.hint
            )
            located = next(
                (i for i, prow in enumerate(picker) if any("selcaret" in s for s, _ in prow)),
                0,
            )
            sel_line = header_lines + located
            rows.extend(picker)
        elif scr.mode == "confirm":
            for text, tone in scr.lines:
                for line in _wrap_line(text, width):
                    rows.append([(_TONE_STYLE.get(tone, "class:setup.text"), line)])
            rows.append([("", "")])
            if scr.hint:
                rows.append([("class:setup.dim", scr.hint)])
        elif scr.mode == "input":
            if scr.input_label:
                rows.append([("class:setup.text", scr.input_label + ":")])
            if scr.input_default:
                rows.append(
                    [("class:setup.dim", f"default: {scr.input_default}  (Enter to accept)")]
                )
        elif scr.mode == "busy":
            frame = _SPINNER_FRAMES[spinner["i"] % len(_SPINNER_FRAMES)]
            detail = report["text"] or scr.busy_label
            rows.append([("class:setup.spinner", f"{frame} "), ("class:setup.text", detail)])

        body_cursor["row"] = sel_line
        out: list[tuple[str, str]] = []
        for row in rows:
            out.extend(row)
            out.append(("", "\n"))
        return FormattedText(out)

    def _status_fragments() -> FormattedText:
        scr = flow.screen()
        if scr.mode == "busy":
            return FormattedText([("", "")])
        if not scr.status:
            return FormattedText([("", "")])
        return FormattedText([(_TONE_STYLE.get(scr.status_tone, "class:setup.dim"), scr.status)])

    def _footer_fragments() -> FormattedText:
        scr = flow.screen()
        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = 80
        left: list[tuple[str, str]] = [("class:setup.footer.progress", scr.progress)]
        # The per-screen hint already lives in the body (picker/confirm/input);
        # keep the footer to the progress label + a global cancel hint.
        right: list[tuple[str, str]] = [("class:setup.footer.dim", "Ctrl+C cancel")]
        left_len = sum(len(t) for _s, t in left)
        right_len = sum(len(t) for _s, t in right)
        gap = max(1, width - left_len - right_len)
        return FormattedText([*left, ("", " " * gap), *right])

    # ----------------------------------------------------------- windows
    # message_window and body_window are focusable so focus can rest on the window
    # that is actually visible for the current screen (no stray cursor parked on a
    # hidden input box). They render no editable cursor (message) / a cursor on the
    # selected row (body), and ignore typed keys — all input is global bindings.
    message_window = Window(
        FormattedTextControl(_message_fragments, focusable=True),
        align=WindowAlign.CENTER,
    )
    body_window = Window(
        FormattedTextControl(
            _body_fragments,
            focusable=True,
            get_cursor_position=lambda: Point(0, body_cursor["row"]),
        ),
        wrap_lines=True,
        scroll_offsets=ScrollOffsets(top=1, bottom=1),
        right_margins=[ScrollbarMargin(display_arrows=False)],
    )
    status_window = Window(FormattedTextControl(_status_fragments, focusable=False), height=1)

    _is_password = Condition(lambda: flow.screen().input_password)
    input_area = TextArea(
        height=1,
        multiline=False,
        wrap_lines=False,
        style="class:tui.input",
        input_processors=[
            ConditionalProcessor(PasswordProcessor(), filter=_is_password),
            BeforeInput("> ", style="class:tui.prompt"),
        ],
    )
    input_frame = Frame(input_area)

    is_message = Condition(lambda: flow.current_mode() == "message")
    is_panel = Condition(lambda: flow.current_mode() in {"list", "input", "confirm", "busy"})
    is_input = Condition(lambda: flow.current_mode() == "input")
    is_busy = Condition(lambda: flow.current_mode() == "busy")
    is_list = Condition(lambda: flow.current_mode() == "list")
    is_confirm = Condition(lambda: flow.current_mode() == "confirm")
    has_status = Condition(lambda: flow.current_mode() != "busy" and bool(flow.screen().status))

    # The input box (and its focus) is the ONLY focusable window, so focus rests on
    # it the whole time — but it is only *shown* on input steps (a hidden
    # ConditionalContainer keeps the window in the layout tree, so focus stays
    # valid and the worker can flip to an input step with no cross-thread focus
    # change). Welcome / picker / confirm screens therefore have no stray text box.
    root = HSplit(
        [
            Window(FormattedTextControl(_header_text, focusable=False), height=1),
            Window(height=1),
            ConditionalContainer(message_window, filter=is_message),
            ConditionalContainer(body_window, filter=is_panel),
            ConditionalContainer(status_window, filter=has_status | is_busy),
            ConditionalContainer(input_frame, filter=is_input),
            Window(height=1),
            Window(FormattedTextControl(_footer_fragments, focusable=False), height=1),
        ]
    )

    # ----------------------------------------------------------- focus / pump
    def _reconcile_focus() -> None:
        # Keep focus on the window that is actually visible for the current
        # screen, so the cursor never parks on a hidden input box.
        mode = flow.current_mode()
        if mode == "input":
            target_window = input_area.window
        elif mode == "message":
            target_window = message_window
        else:
            target_window = body_window
        try:
            app = get_app()
            if app.layout.current_window is not target_window:
                app.layout.focus(target_window)
        except Exception:
            pass

    def _schedule_focus() -> None:
        # Move focus from a worker thread safely (only app.invalidate() and
        # loop.call_soon_threadsafe are thread-safe in prompt_toolkit).
        try:
            loop = getattr(get_app(), "loop", None)
            if loop is not None:
                loop.call_soon_threadsafe(_reconcile_focus)
        except Exception:
            pass

    def _busy_worker() -> None:
        while True:
            if flow.current_mode() != "busy" or flow.busy_kind() != "thread":
                break
            try:
                flow.run_busy()
            except BaseException as exc:  # noqa: BLE001 - surface any failure as fatal
                flow.fatal_error = f"{type(exc).__name__}: {exc}"
                flow.stage = "fatal"
            report["text"] = ""
            _invalidate()
        busy["running"] = False
        _schedule_focus()
        _invalidate()

    async def _terminal_busy() -> None:
        try:
            await run_in_terminal(flow.run_busy, in_executor=True)
        except BaseException as exc:  # noqa: BLE001
            flow.fatal_error = f"{type(exc).__name__}: {exc}"
            flow.stage = "fatal"
        report["text"] = ""
        busy["running"] = False
        _invalidate()
        _pump()

    def _pump() -> None:
        # Exit once the flow terminates.
        if flow.stage == "done":
            try:
                get_app().exit(result=bool(flow.success))
            except Exception:
                pass
            return
        mode = flow.current_mode()
        # Clear the input box once per stage change (UI thread only) so a prior
        # step's text never lingers. We never *seed* the buffer — defaults are
        # shown as a hint and the flow treats empty input as the default — so a
        # worker flipping to an input step (key-retry / workspace) needs no
        # cross-thread buffer write: the box was already cleared when its busy
        # step began.
        if flow.stage != last_stage["v"]:
            if mode != "input" or not input_area.text:
                input_area.text = ""
            last_stage["v"] = flow.stage
        _reconcile_focus()
        # Dispatch a busy step to the right executor.
        if mode == "busy" and not busy["running"]:
            busy["running"] = True
            report["text"] = ""
            if inline_busy:
                while flow.current_mode() == "busy":
                    try:
                        flow.run_busy()
                    except BaseException as exc:  # noqa: BLE001
                        flow.fatal_error = f"{type(exc).__name__}: {exc}"
                        flow.stage = "fatal"
                    report["text"] = ""
                busy["running"] = False
                _pump()  # handle whatever stage the chain landed on
                return
            if flow.busy_kind() == "thread":
                threading.Thread(target=_busy_worker, daemon=True).start()
            else:
                try:
                    get_app().create_background_task(_terminal_busy())
                except Exception:
                    # No running loop — run inline as a fallback, then re-pump so
                    # the stage it lands on (input prep / exit / a chained busy
                    # step) is handled, exactly like the other two busy paths.
                    try:
                        flow.run_busy()
                    except BaseException as exc:  # noqa: BLE001
                        flow.fatal_error = f"{type(exc).__name__}: {exc}"
                        flow.stage = "fatal"
                    busy["running"] = False
                    _pump()
                    return
        _invalidate()

    # ----------------------------------------------------------- key bindings
    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-d")
    def _cancel(event: Any) -> None:
        # Always abort setup immediately so the user is never trapped — including
        # mid-validation (the daemon worker, if any, dies with the process) and on
        # the cancel-confirm screen. Returns ``False`` (cancelled) to the caller.
        flow.success = False
        try:
            event.app.exit(result=False)
        except Exception:
            pass

    @kb.add("enter", eager=True, filter=~is_busy)
    def _enter(event: Any) -> None:
        mode = flow.current_mode()
        if mode == "input":
            flow.submit_input(input_area.text)
        elif mode == "list":
            flow.choose_current()
        elif mode == "message":
            flow.advance_message()
        elif mode == "confirm":
            flow.confirm(flow.screen().confirm_default)
        _pump()

    @kb.add("escape", filter=~is_busy)
    def _escape(event: Any) -> None:
        flow.back()
        _pump()

    @kb.add("up", filter=is_list, eager=True)
    @kb.add("k", filter=is_list, eager=True)
    def _up(event: Any) -> None:
        flow.move(-1)
        _invalidate()

    @kb.add("down", filter=is_list, eager=True)
    @kb.add("j", filter=is_list, eager=True)
    def _down(event: Any) -> None:
        flow.move(1)
        _invalidate()

    for _digit in "123456789":

        @kb.add(_digit, filter=is_list, eager=True)
        def _pick(event: Any) -> None:
            try:
                flow.choose_index(int(event.data) - 1)
            except (TypeError, ValueError):
                return
            _pump()

    @kb.add("y", filter=is_confirm, eager=True)
    @kb.add("Y", filter=is_confirm, eager=True)
    def _yes(event: Any) -> None:
        flow.confirm(True)
        _pump()

    @kb.add("n", filter=is_confirm, eager=True)
    @kb.add("N", filter=is_confirm, eager=True)
    def _no(event: Any) -> None:
        flow.confirm(False)
        _pump()

    app: Application = Application(
        layout=Layout(root, focused_element=message_window),
        key_bindings=kb,
        style=_STYLE,
        full_screen=True,
        mouse_support=False,
        cursor=CursorShape.BEAM,
        input=input,
        output=output,
    )

    def _pre_run() -> None:
        # Prime the very first screen (start any immediate busy step / input box).
        _pump()

        def _spin() -> None:
            while getattr(app, "is_running", False):
                time.sleep(0.1)
                animated = False
                if owl.available and flow.current_mode() == "message" and flow.stage == "welcome":
                    owl.advance()
                    animated = True
                if flow.current_mode() == "busy":
                    spinner["i"] = (spinner["i"] + 1) % len(_SPINNER_FRAMES)
                    animated = True
                if animated:
                    try:
                        app.invalidate()
                    except Exception:
                        break

        threading.Thread(target=_spin, daemon=True).start()

    result = app.run(pre_run=_pre_run)
    return bool(result)


__all__ = ["run_setup_tui"]
