from __future__ import annotations

import asyncio
import threading
import traceback
from urllib.parse import quote

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.tui import app as app_module
from alysis_code.cli_impl.tui.links import link_at, link_target
from alysis_code.cli_impl.tui.state import TuiState


@pytest.mark.parametrize(
    "reply,url,label",
    [
        (
            "Open **http://127.0.0.1:8734** in your browser.",
            "http://127.0.0.1:8734",
            "http://127.0.0.1:8734",
        ),
        (
            "Δείτε [προεπισκόπηση](http://localhost:5173/διαδρομή?q=ναι#α).",
            "http://localhost:5173/διαδρομή?q=ναι#α",
            "προεπισκόπηση",
        ),
        (
            "Visit https://example.org/a_(b)?q=yes&x=2#section.",
            "https://example.org/a_(b)?q=yes&x=2#section",
            "https://example.org/a_(b)?q=yes&x=2#section",
        ),
        ("Open (http://[::1]:8080/).", "http://[::1]:8080/", "http://[::1]:8080/"),
        (
            "[https://label.example](https://target.example/path)",
            "https://target.example/path",
            "https://label.example",
        ),
        ("Open `http://localhost:3000` now.", "http://localhost:3000", "http://localhost:3000"),
        ("[Preview][site]\n\n[site]: http://localhost:3000", "http://localhost:3000", "Preview"),
    ],
)
@pytest.mark.parametrize("width", [8, 24, 100])
def test_links_preserve_full_targets_through_markdown_and_wrapping(reply, url, label, width):
    rows = app_module._assistant_rows(reply, width)
    linked_text = []
    for row in rows:
        text = "".join(value for _, value in row)
        for x, char in enumerate(text):
            target = link_at(row, x)
            if target:
                assert target == quote(url, safe=":/?&=#%()[]")
                linked_text.append(char)
    assert "".join(linked_text) == label
    assert all("\x1b" not in text and "8;id=" not in text for row in rows for _, text in row)
    assert "**" not in "".join(text for row in rows for _, text in row)


def test_links_are_not_invented_in_code_or_unsafe_schemes():
    for reply in (
        "[run](file:///tmp/script)",
        "[run](javascript:alert%281%29)",
        "[run](https://)",
    ):
        rows = app_module._assistant_rows(reply)
        assert not any(
            [link_target(style) for style, _text in row if link_target(style)] for row in rows
        )


def test_streaming_reply_stays_plain_until_complete():
    reply = "Open **http://localhost:3000**"
    rows = app_module._assistant_rows(reply, markdown=False)
    assert "**" in "".join(text for row in rows for _, text in row)


@pytest.mark.parametrize(
    "reply,label,url,columns,click_offset",
    [
        (
            "It's live — open **http://127.0.0.1:8734** in your browser.",
            "http://",
            "http://127.0.0.1:8734",
            80,
            0,
        ),
        (
            "界 Δείτε [Preview](http://localhost:5173/path?q=1) now.",
            "Preview",
            "http://localhost:5173/path?q=1",
            40,
            0,
        ),
        ("Open [界面](http://localhost:8080) now.", "界面", "http://localhost:8080", 60, 1),
    ],
)
def test_terminal_link_clicks_preserve_drag_selection_and_input(
    monkeypatch, reply, label, url, columns, click_offset
):
    rendered = {"screen": None}
    dimensions = {"columns": columns}
    opened = []
    errors = []
    browser_release = threading.Event()

    class SizedOutput(DummyOutput):
        def get_size(self):
            return Size(rows=30, columns=dimensions["columns"])

    class Session:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None):
            self.surface.on_user_message(text)
            self.surface.on_assistant_message_done(reply)
            return 0

    def position():
        screen = rendered["screen"]
        if screen is not None:
            for y in range(30):
                for x in range(dimensions["columns"]):
                    tail = "".join(
                        screen.data_buffer[y][i].char for i in range(x, dimensions["columns"])
                    )
                    if tail.startswith(label):
                        return x + 1 + click_offset, y + 1
        return None

    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def exercise(application, pipe):
        try:
            pipe.send_text("Serve the page\r")
            await wait_for(position)
            x, y = position()
            input_buffer = application.layout.current_buffer

            def mouse(button, x, y, release=False):
                pipe.send_text(f"\x1b[<{button};{x};{y}{'m' if release else 'M'}")

            # Right click and an unmatched release never launch a browser.
            mouse(2, x, y)
            mouse(2, x, y, True)
            mouse(0, x, y, True)
            await asyncio.sleep(0.06)
            assert opened == []

            # Drag selection, including a drag out and back to the same cell.
            for return_to_start in (False, True):
                mouse(0, x, y)
                await asyncio.sleep(0.05)
                mouse(32, x + 4, y)
                if return_to_start:
                    mouse(32, x, y)
                mouse(0, x if return_to_start else x + 4, y, True)
                await asyncio.sleep(0.06)
                assert opened == []
            # Scrolling or resizing during a press cancels link activation.
            mouse(0, x, y)
            await asyncio.sleep(0.05)
            mouse(64, x, y)
            mouse(0, x, y, True)
            await asyncio.sleep(0.06)
            assert opened == []
            mouse(0, x, y)
            await asyncio.sleep(0.05)
            dimensions["columns"] += 5
            application.invalidate()
            await asyncio.sleep(0.06)
            mouse(0, x, y, True)
            await asyncio.sleep(0.06)
            assert opened == []
            x, y = position()
            # Holding over a repaint exercises the capture overlay; immediate
            # down/up exercises the original control before it can repaint.
            for hold in (0, 0.06):
                if hold:
                    await asyncio.sleep(app_module._LINK_REOPEN_GUARD_SECONDS)
                mouse(0, x, y)
                if hold:
                    await asyncio.sleep(hold)
                mouse(0, x, y, True)
                expected_count = 1 if hold == 0 else 2
                await wait_for(lambda count=expected_count: len(opened) == count)
            assert opened == [(url, True), (url, True)]
            assert application.layout.current_buffer is input_buffer
            # Browser launch is still blocked, but typing remains responsive.
            pipe.send_text("still typing")
            await wait_for(lambda: input_buffer.text == "still typing")
        except Exception as exc:
            errors.append((str(exc), traceback.format_exc(), opened.copy()))
        finally:
            browser_release.set()
            application.exit()

    def capture_application(*args, **kwargs):
        application = Application(
            *args,
            **kwargs,
            after_render=lambda app: rendered.update(screen=app.renderer.last_rendered_screen),
        )
        application.pre_run_callables.append(
            lambda: application.create_background_task(exercise(application, pipe))
        )
        return application

    def open_browser(target, *, quiet=False):
        opened.append((target, quiet))
        browser_release.wait(3)
        return True

    monkeypatch.setattr(app_module, "Application", capture_application)
    monkeypatch.setattr(app_module, "open_url", open_browser)
    with create_pipe_input() as pipe:
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=SizedOutput(),
            session_builder=Session,
        )
    assert not errors, errors


def test_no_color_preserves_clickable_links_without_syntax_colors(monkeypatch):
    from alysis_code.cli_impl.tui.markdown import _render_ansi, render_markdown_rows

    monkeypatch.setenv("NO_COLOR", "1")
    _render_ansi.cache_clear()
    render_markdown_rows.cache_clear()
    try:
        rows = app_module._assistant_rows(
            "Open [Preview](http://localhost:3000).\n\n```python\ndef f(x):\n    return x + 1\n```",
            theme="dark",
        )
        styles = [style for row in rows for style, _text in row]
        assert not any("#" in style or "bg:" in style or "ansicolor" in style for style in styles)
        assert any(
            url == "http://localhost:3000"
            for row in rows
            for url in [link_target(style) for style, _text in row if link_target(style)]
        )
    finally:
        _render_ansi.cache_clear()
        render_markdown_rows.cache_clear()


@pytest.mark.parametrize(
    "middle,visible",
    [
        ("See https://example.com", "See https://example.com"),
        ("See **https://example.com**", "See https://example.com"),
        ("See [Preview](https://example.com)", "See Preview"),
    ],
)
def test_linkifying_prose_preserves_explicit_line_breaks(middle, visible):
    rows = app_module._assistant_rows(f"First line\n{middle}\nThird line", width=80)
    assert ["".join(text for _style, text in row)[2:] for row in rows] == [
        "First line",
        visible,
        "Third line",
    ]
    targets = [
        url
        for row in rows
        for url in [link_target(style) for style, _text in row if link_target(style)]
    ]
    assert targets and set(targets) == {"https://example.com"}
