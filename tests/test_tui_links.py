"""Clickable links in the TUI transcript and popups.

The full-screen TUI owns the mouse, so the terminal never acts on a URL: the app
finds links itself (``links.py``), carries each target on its fragments' style,
and opens it in the browser on a plain left click.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style

from alysis_code.cli_impl.tui import app as app_module
from alysis_code.cli_impl.tui.app import (
    _assistant_rows,
    _plain_role_rows,
    _reasoning_rows,
    _render_doc_panel_rows,
    _status_line_fragments,
    _user_band_rows,
    _wrap_line,
)
from alysis_code.cli_impl.tui.links import (
    browser_url,
    find_urls,
    link_at,
    link_segments,
    link_style,
    link_target,
    pad_after_trailing_link,
)
from alysis_code.cli_impl.tui.markdown import render_markdown_rows
from alysis_code.cli_impl.tui.state import TuiState


def _text(row: list[tuple[str, str]]) -> str:
    return "".join(text for _style, text in row)


def _link_runs(rows: list[list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """``(shown text, target)`` per linked run, merging a run's fragments."""
    runs: list[tuple[str, str]] = []
    for row in rows:
        previous = None
        for style, text in row:
            target = link_target(style)
            if target is not None and target == previous:
                runs[-1] = (runs[-1][0] + text, target)
            elif target is not None:
                runs.append((text, target))
            previous = target
    return runs


# ------------------------------- detection -------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see https://example.com.", [("https://example.com", "https://example.com")]),
        (
            "(docs: https://en.wikipedia.org/wiki/Foo_(bar))",
            [
                (
                    "https://en.wikipedia.org/wiki/Foo_(bar)",
                    "https://en.wikipedia.org/wiki/Foo_(bar)",
                )
            ],
        ),
        (
            "[https://a.example](https://b.example)",
            [
                ("https://a.example", "https://a.example"),
                ("https://b.example", "https://b.example"),
            ],
        ),
        (
            "**https://bold.example.com**",
            [("https://bold.example.com", "https://bold.example.com")],
        ),
        (
            "'https://quoted.example.com'",
            [("https://quoted.example.com", "https://quoted.example.com")],
        ),
        ("then open localhost:5173/app", [("localhost:5173/app", "http://localhost:5173/app")]),
        (
            "Flask on 127.0.0.1:5000, not localhost alone",
            [("127.0.0.1:5000", "http://127.0.0.1:5000")],
        ),
        ("visit www.example.org/docs!", [("www.example.org/docs", "https://www.example.org/docs")]),
        ("IPv6 at http://[::1]:8000/x", [("http://[::1]:8000/x", "http://[::1]:8000/x")]),
        (
            "pip install git+https://github.com/o/r.git",
            [("https://github.com/o/r.git", "https://github.com/o/r.git")],
        ),
    ],
)
def test_find_urls_leaves_surrounding_prose_out_of_the_link(text, expected):
    assert [(text[m.start : m.end], m.target) for m in find_urls(text)] == expected


@pytest.mark.parametrize(
    "text",
    [
        # Tool traces elide long arguments; the visible prefix is the wrong page.
        "✓ Web Fetch · https://example.com/some/very/long/path/that/gets/clip… (1.2s)",
        "middle elided: https://example.com/…/deep/file.txt",
        "cut short: https://example.com/path... and more",
        # Only http(s) ever reaches the OS opener.
        "ftp://files.example.com file:///etc/passwd javascript:alert(1) ms-msdt:/id",
        "https:// and http:// alone",
        "mail me@www.example.com",
    ],
)
def test_find_urls_refuses_elided_unsafe_and_hostless_urls(text):
    assert find_urls(text) == []


def test_browser_url_opens_any_address_servers_on_localhost():
    assert browser_url("http://0.0.0.0:8000/x?y=1") == "http://localhost:8000/x?y=1"
    assert browser_url("http://[::]:9000/") == "http://localhost:9000/"
    assert browser_url("http://127.0.0.1:5000") == "http://127.0.0.1:5000"
    assert browser_url("https://example.com/a") == "https://example.com/a"


def test_link_style_round_trips_targets_prompt_toolkit_could_misread():
    url = "http://[::1]:8000/a%20b?q[]=1#[transparent]"
    style = link_style(url, "class:tui.transcript.trace")

    assert link_target(style) == url
    # prompt_toolkit substring-tests a few bracketed tokens; the target can't form one.
    assert "[transparent]" not in style
    assert style.startswith("class:tui.transcript.trace class:tui.transcript.link ")
    attrs = Style.from_dict({"tui.transcript.link": "underline"}).get_attrs_for_style_str(style)
    assert attrs.underline


def test_link_segments_keep_the_whole_target_on_every_wrapped_piece():
    url = "https://github.com/example-org/example-repo/blob/main/src/package/module.py#L10-L20"
    line = f"Source: {url}."
    chunks = _wrap_line(line, 24)
    assert len(chunks) > 2

    pieces = [
        (text, target)
        for segments in link_segments(line, chunks)
        for text, target in segments
        if target
    ]

    assert "".join(text for text, _target in pieces) == url
    assert {target for _text, target in pieces} == {url}


def test_pad_after_trailing_link_only_pads_a_row_ending_in_a_link_that_fits():
    row = [("", "see "), (link_style("https://example.com"), "https://example.com")]

    padded = pad_after_trailing_link(row, 40)

    assert padded == [*row, ("", " ")]
    # The blank cell beside the link is not the link.
    assert link_at(padded, len(_text(row))) is None
    assert link_at(padded, len(_text(row)) - 1) == "https://example.com"
    assert pad_after_trailing_link(row, len(_text(row))) is row  # full width: no room
    plain = [("", "no link here")]
    assert pad_after_trailing_link(plain, 40) is plain


# ------------------------------ row builders ------------------------------


def test_plain_role_rows_link_urls_and_leave_other_rows_untouched():
    rows = _plain_role_rows(
        "class:tui.transcript.system",
        "Server ready at http://localhost:3000\nno links here",
        80,
    )

    assert _text(rows[0]) == "Server ready at http://localhost:3000"
    assert link_at(rows[0], len("Server ready at ")) == "http://localhost:3000"
    assert link_at(rows[0], 0) is None
    assert rows[1] == [("class:tui.transcript.system", "no links here")]


def test_streaming_assistant_rows_link_urls_behind_the_marker():
    rows = _assistant_rows("Open https://example.com/app now\nand more", width=60, markdown=False)

    assert _text(rows[0]) == "✦ Open https://example.com/app now"
    assert link_at(rows[0], len("✦ Open ")) == "https://example.com/app"
    assert rows[1] == [("class:tui.transcript.assistant", "  and more")]


def test_user_band_links_keep_the_band_full_width():
    rows = _user_band_rows("check https://example.com please", 50)

    assert len(_text(rows[1])) == 50
    assert link_at(rows[1], len("› check ")) == "https://example.com"
    assert all("userband" in style or "userprompt" in style for style, _text in rows[1])
    # A message without a URL renders exactly as before.
    assert _user_band_rows("hello", 20)[1] == [
        ("class:tui.transcript.userprompt", "› "),
        ("class:tui.transcript.userband", "hello".ljust(18)),
    ]


def test_reasoning_body_links_urls():
    rows = _reasoning_rows(
        "checking https://docs.example.com first", 60, live=False, secs=1, expanded=True
    )

    assert link_at(rows[1], len("│ checking ")) == "https://docs.example.com"


def test_plain_doc_panel_links_urls():
    rows = _render_doc_panel_rows("plain doc https://example.com/doc here", 60, "")

    assert link_at(rows[0], len("plain doc ")) == "https://example.com/doc"


# -------------------------------- markdown --------------------------------

_MD_REPLY = (
    "Sources:\n\n"
    "- Spain ([apnews.com](https://apnews.com/article/d47ccb4ac5b3af67?utm_source=openai))\n"
    "- Guide: [**the** docs](https://docs.example.com/guide)\n"
    "- Bare: https://example.com/bare, and `http://localhost:5173` in code\n"
    "- Relative: [readme](./README.md)\n"
    "\n```\nLocal:   http://localhost:5173/\n```\n"
)


def test_markdown_links_carry_their_targets():
    rows = render_markdown_rows(_MD_REPLY, 70)
    assert rows is not None

    runs = _link_runs(rows)

    assert ("apnews.com", "https://apnews.com/article/d47ccb4ac5b3af67?utm_source=openai") in runs
    # Bold text inside an anchor is still one link.
    assert ("the docs", "https://docs.example.com/guide") in runs
    assert ("https://example.com/bare", "https://example.com/bare") in runs
    assert ("http://localhost:5173", "http://localhost:5173") in runs  # inline code
    assert ("http://localhost:5173/", "http://localhost:5173/") in runs  # fenced code
    assert all(not target.endswith("README.md") for _text, target in runs)


def test_markdown_link_markers_never_reach_the_screen():
    rows = render_markdown_rows(_MD_REPLY, 70)
    assert rows is not None

    joined = "\n".join(_text(row) for row in rows)

    assert "\x01" not in joined and "\x02" not in joined
    assert "alysis-link" not in joined
    assert "https://apnews.com" not in joined  # an anchor shows its text, not its URL
    for row in rows:
        assert len(_text(row)) <= 70
        assert all("[ZeroWidthEscape]" not in style for style, _text in row)


def test_markdown_url_folded_across_rows_opens_whole_from_any_row():
    url = "https://github.com/example-org/example-repository/blob/main/src/very/long/module.py"
    rows = render_markdown_rows(f"# Where\n\nSee {url} now.\n", 30)
    assert rows is not None

    runs = _link_runs(rows)

    assert len(runs) > 1  # Rich folded it
    assert "".join(text for text, _target in runs) == url
    assert {target for _text, target in runs} == {url}


# ------------------------------- status line -------------------------------


def test_status_notice_outranks_the_running_reminder():
    def plain(fragments):
        return "".join(text for _style, text in fragments)

    assert plain(_status_line_fragments(running=True, notice="Opening https://example.com")) == (
        "  Opening https://example.com"
    )
    assert "Esc or Ctrl+C to interrupt" in plain(_status_line_fragments(running=True))


def test_link_fallback_copies_the_url_when_no_browser_opens(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr(app_module, "copy_text_to_clipboard", copied.append)

    assert app_module._link_fallback_notice("https://example.com") == (
        "Couldn't open a browser · link copied to the clipboard"
    )
    assert copied == ["https://example.com"]

    def _fail(_text: str) -> None:
        raise app_module.ClipboardError("unavailable")

    monkeypatch.setattr(app_module, "copy_text_to_clipboard", _fail)
    assert app_module._link_fallback_notice("https://example.com") == (
        "Couldn't open a browser for https://example.com"
    )


# ------------------------------ live clicks ------------------------------

_COLUMNS, _ROWS = 100, 30
_REPLY = (
    "Here you go:\n\n"
    "- Docs: [the docs](https://docs.example.com/guide)\n"
    "- Local: http://localhost:5173/\n"
    "- 🚀 Any: http://0.0.0.0:8000/ ready\n"
)


@dataclass(frozen=True)
class _Cell:
    x: int
    y: int

    def shift(self, dx: int) -> _Cell:
        return _Cell(self.x + dx, self.y)


def _press(cell: _Cell) -> str:
    return f"\x1b[<0;{cell.x + 1};{cell.y + 1}M"


def _drag(cell: _Cell) -> str:
    return f"\x1b[<32;{cell.x + 1};{cell.y + 1}M"


def _release(cell: _Cell) -> str:
    return f"\x1b[<0;{cell.x + 1};{cell.y + 1}m"


class _Harness:
    def __init__(self, application: Application, pipe, opened: list[tuple[str, bool]]):
        self.application = application
        self.pipe = pipe
        self.calls = opened
        self.cells: list[list[tuple[int, str]]] = []

    @property
    def opened(self) -> list[str]:
        return [url for url, _quiet in self.calls]

    def capture(self) -> None:
        screen = self.application.renderer.last_rendered_screen
        if screen is not None:
            self.cells = [
                [(x, screen.data_buffer[y][x].char) for x in range(_COLUMNS)] for y in range(_ROWS)
            ]

    def text(self) -> str:
        return "\n".join("".join(char for _x, char in row).rstrip() for row in self.cells)

    def find(self, needle: str) -> _Cell | None:
        """Screen cell of ``needle``'s first character (wide characters included)."""
        for y, row in enumerate(self.cells):
            xs = [x for x, char in row if char]
            line = "".join(char for _x, char in row if char)
            index = line.find(needle)
            if index >= 0:
                return _Cell(xs[index], y)
        return None

    def send(self, data: str) -> None:
        self.pipe.send_text(data)

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def click(self, cell: _Cell, *, fast: bool) -> None:
        if fast:
            # One input batch, like a touchpad tap: the release arrives before
            # the drag-capture overlay has painted.
            self.send(_press(cell) + _release(cell))
        else:
            # Held through a repaint: the release goes through the overlay.
            self.send(_press(cell))
            await asyncio.sleep(0.05)
            self.send(_release(cell))
        await asyncio.sleep(0.05)


class _ReplySession:
    def __init__(self, surface) -> None:
        self.surface = surface

    def run_turn(self, text: str, *, cancellation_token=None, **_kwargs) -> int:
        self.surface.on_user_message(text)
        self.surface.on_assistant_message_done(_REPLY)
        return 0


def _run_with_mouse(
    monkeypatch,
    exercise: Callable[[_Harness], Awaitable[None]],
    **run_kwargs,
) -> list[tuple[str, bool]]:
    opened: list[tuple[str, bool]] = []
    errors: list[BaseException] = []
    harness: dict[str, _Harness] = {}

    class _SizedOutput(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=_ROWS, columns=_COLUMNS)

    async def _drive(application: Application, pipe) -> None:
        try:
            await exercise(harness["h"])
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)
        finally:
            application.exit()

    def capture_application(*args, **kwargs):
        application = Application(*args, **kwargs, after_render=lambda _app: harness["h"].capture())
        harness["h"] = _Harness(application, pipe, opened)
        application.pre_run_callables.append(
            lambda: application.create_background_task(_drive(application, pipe))
        )
        return application

    def open_browser(url: str, *, quiet: bool = False) -> bool:
        opened.append((url, quiet))
        return True

    monkeypatch.setattr(app_module, "Application", capture_application)
    monkeypatch.setattr(app_module, "open_url", open_browser)
    with create_pipe_input() as pipe:
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=_SizedOutput(),
            **run_kwargs,
        )
    assert not errors, (errors, harness["h"].text())
    return opened


def test_clicking_transcript_links_opens_them_in_the_browser(monkeypatch):
    async def exercise(h: _Harness) -> None:
        await h.wait_for(lambda: bool(h.cells))
        h.send("links please\r")
        await h.wait_for(lambda: h.find("http://0.0.0.0:8000/") is not None)
        docs = h.find("the docs")
        local = h.find("http://localhost:5173/")
        any_address = h.find("http://0.0.0.0:8000/")
        assert docs is not None and local is not None and any_address is not None

        # A right click is not a link click.
        h.send(f"\x1b[<2;{docs.x + 1};{docs.y + 1}M\x1b[<2;{docs.x + 1};{docs.y + 1}m")
        await asyncio.sleep(0.05)
        assert h.opened == []

        # A markdown anchor opens its href, even when the release beats the overlay.
        await h.click(docs.shift(2), fast=True)
        await h.wait_for(lambda: h.opened == ["https://docs.example.com/guide"])
        await h.wait_for(lambda: "Opening https://docs.example.com/guide" in h.text())

        # A double click opens the page once.
        await h.click(docs.shift(2), fast=True)
        assert h.opened == ["https://docs.example.com/guide"]

        # A press held through a repaint is settled by the capture overlay.
        await h.click(local.shift(4), fast=False)
        await h.wait_for(lambda: h.opened[-1:] == ["http://localhost:5173/"])

        # A wide emoji earlier in the row must not shift the hit test, and a
        # server on every interface opens on localhost.
        await h.click(any_address.shift(3), fast=False)
        await h.wait_for(lambda: h.opened[-1:] == ["http://localhost:8000/"])
        await h.wait_for(lambda: "Opening http://localhost:8000/" in h.text())
        opened_so_far = len(h.calls)

        # Dragging across a link selects text instead of opening it.
        h.send(_press(local))
        await asyncio.sleep(0.05)
        h.send(_drag(local.shift(6)))
        await asyncio.sleep(0.05)
        h.send(_release(local.shift(6)))
        await asyncio.sleep(0.05)
        # Leaving the link and coming back to it cancels the click too.
        h.send(_press(local))
        await asyncio.sleep(0.05)
        h.send(_drag(local.shift(8)))
        await asyncio.sleep(0.05)
        h.send(_drag(local))
        await asyncio.sleep(0.05)
        h.send(_release(local))
        await asyncio.sleep(0.05)
        # ... also when the whole gesture lands before the overlay paints.
        h.send(_press(local) + _drag(local.shift(8)) + _drag(local) + _release(local))
        await asyncio.sleep(0.05)
        # The blank space beside a row that ends in a link is not the link.
        beside = local.shift(len("http://localhost:5173/") + 4)
        await h.click(beside, fast=True)
        await h.click(beside, fast=False)
        await asyncio.sleep(0.05)
        assert len(h.calls) == opened_so_far

    calls = _run_with_mouse(monkeypatch, exercise, session_builder=_ReplySession)

    # Launched quietly so a browser helper cannot scribble over the alt-screen.
    assert calls and all(quiet for _url, quiet in calls)


def test_clicking_a_link_in_a_doc_panel_opens_it(monkeypatch):
    url = "https://example.com/design/notes"
    body = f"{url} is where the notes live.\n\n- first item\n- second item\n"

    async def exercise(h: _Harness) -> None:
        await h.wait_for(lambda: bool(h.cells))
        h.send("/doc\r")
        await h.wait_for(lambda: h.find(url) is not None)
        target = h.find(url)
        assert target is not None

        # prompt_toolkit reports a click on an empty row as (0, 0) — where this
        # document's first link starts — so blank rows must not open it.
        await h.click(_Cell(target.x + 4, target.y + 1), fast=True)
        assert h.opened == []

        await h.click(target.shift(4), fast=True)
        await h.wait_for(lambda: h.opened == [url])

    _run_with_mouse(
        monkeypatch,
        exercise,
        session_builder=_ReplySession,
        panel_providers={"/doc": lambda _arg="": {"title": "Doc", "body": body}},
    )
