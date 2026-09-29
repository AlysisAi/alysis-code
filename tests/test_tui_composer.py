"""The TUI composer's word wrap, its row navigation, and the responsive landing.

prompt_toolkit wraps an input line at whichever character reaches the right
edge, so in a window narrower than full screen the composer used to split words
across rows ("… of this c" / "omposer …") and start each continuation at
column 0, under the prompt. These tests draw through prompt_toolkit's real
renderer, so they check what reaches the screen rather than the layout
arithmetic alone.
"""

from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.tui import app as app_module
from alysis_code.cli_impl.tui.composer import ComposerWrapProcessor, vertical_move, wrap_cells
from alysis_code.cli_impl.tui.content import (
    CREDIT_TEXT,
    HEADING_TEXT,
    HINT_TEXT,
    INPUT_PLACEHOLDER,
    INPUT_PLACEHOLDER_FOLLOWUP,
    WELCOME_SEPARATOR,
    welcome_text_rows,
)
from alysis_code.cli_impl.tui.state import TuiState

SENTENCE = (
    "Can you please check the professional wrapping of this composer text box "
    "when the window is not full screen and the message keeps going"
)


def _sized_output(columns: int, rows: int) -> DummyOutput:
    class _Output(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=rows, columns=columns)

    return _Output()


def _paint(app: Application, columns: int, rows: int) -> tuple[list[str], object]:
    """Render ``app`` once and return its screen rows (right-stripped) and screen.

    A BufferControl starts loading its history as a background task when it is
    drawn, which needs a running event loop.
    """

    async def _render():
        with set_app(app):
            app.renderer.render(app, app.layout)
            screen = app.renderer.last_rendered_screen
            return [
                "".join(screen.data_buffer[y][x].char for x in range(columns)).rstrip()
                for y in range(rows)
            ], screen

    return asyncio.run(_render())


def _draw(text: str, width: int, *, cursor: int | None = None, height: int = 16):
    """Draw ``text`` in a composer window ``width`` cells wide.

    Returns the non-blank screen rows (right-stripped), the screen cursor, and
    the window's preferred height, which is what sizes the composer's frame.
    """
    position = len(text) if cursor is None else cursor
    buffer = Buffer(document=Document(text, position), multiline=True)
    control = BufferControl(buffer=buffer, input_processors=[ComposerWrapProcessor()])
    window = Window(control, wrap_lines=True)
    with create_pipe_input() as pipe:
        app = Application(
            layout=Layout(window),
            input=pipe,
            output=_sized_output(width, height),
            full_screen=True,
        )
        rows, screen = _paint(app, width, height)
        with set_app(app):
            preferred = window.preferred_height(width, 1000).preferred
    while rows and not rows[-1]:
        rows.pop()
    return rows, screen.get_cursor_position(window), preferred


# ------------------------------------------------------------------ word wrap


@pytest.mark.parametrize("width", [18, 24, 31, 40, 44, 58, 60, 77])
def test_composer_never_splits_a_word_across_rows(width):
    rows, _cursor, preferred = _draw(SENTENCE, width)

    assert len(rows) > 1
    assert rows[0].startswith("> ")
    # Continuation rows hang under the text, not under the prompt.
    assert all(row.startswith("  ") and row[2] != " " for row in rows[1:])
    # Re-joining the rows gives back exactly the original words: a word split
    # over two rows would come back as two fragments.
    assert " ".join(row[2:].strip() for row in rows).split() == SENTENCE.split()
    # One column stays free on the right, so text never touches the frame.
    assert all(len(row) <= width - 1 for row in rows)
    # The frame is sized for exactly the rows drawn.
    assert preferred == len(rows)


def test_composer_matches_the_reported_narrow_window():
    # A 60-column terminal: the frame's borders leave 58 cells per row.
    rows, cursor, _preferred = _draw(SENTENCE[:100], 58)

    assert rows == [
        "> Can you please check the professional wrapping of this",
        "  composer text box when the window is not full",
    ]
    assert (cursor.y, cursor.x) == (1, len(rows[1]))


def test_composer_hard_breaks_a_token_longer_than_a_row():
    url = "https://example.com/" + "segment/" * 12
    rows, cursor, preferred = _draw(f"see {url}", 30)

    # The URL starts on the first row: moving it would cost an extra row.
    assert rows[0] == "> see https://example.com/seg"
    assert "".join(row[2:] for row in rows) == f"see {url}"
    assert all(len(row) <= 29 for row in rows)
    assert preferred == len(rows)
    assert (cursor.y, cursor.x) == (len(rows) - 1, len(rows[-1]))


def test_composer_moves_a_long_token_to_a_fresh_row_when_that_costs_nothing():
    # Starting the token on the first row would still need two more rows, so it
    # starts on its own row instead of leaving one character behind.
    widths = [1] * 10 + [1] + [1] * 30
    spaces = [False] * 10 + [True] + [False] * 30
    rows = wrap_cells(widths, spaces, [False] * len(widths), first_cap=12, rest_cap=15)

    assert [(row.start, row.end) for row in rows] == [(0, 11), (11, 26), (26, 41)]


def test_composer_cursor_gets_its_own_row_after_a_space_in_the_last_column():
    # 29 cells: the prompt, 26 cells of text, and the column kept free.
    text = "a" * 26 + " "
    rows, cursor, preferred = _draw(text, 29)

    assert rows == ["> " + "a" * 26]
    # The space took the column the cursor would need, so the cursor starts
    # the next row at the text column, where the next word will go.
    assert (cursor.y, cursor.x) == (1, 2)
    assert preferred == 2


def test_composer_indents_every_hard_line_under_the_first():
    rows, cursor, _preferred = _draw("first line\nsecond line\n\nfourth", 40)

    assert rows == ["> first line", "  second line", "", "  fourth"]
    assert (cursor.y, cursor.x) == (3, 8)


def test_composer_wraps_wide_glyphs_without_overflowing_the_row():
    text = "寫一個函數計算費波那契數列並且加上完整的單元測試與文件說明"
    rows, _cursor, preferred = _draw(text, 20)

    assert len(rows) > 1
    assert "".join(row[2:] for row in rows) == text
    assert preferred == len(rows)


def test_composer_draws_a_tab_as_blank_cells():
    rows, _cursor, _preferred = _draw("def f():\n\treturn 1", 40)

    assert rows == ["> def f():", "      return 1"]
    assert "^I" not in "".join(rows)


def test_composer_positions_round_trip_through_the_layout():
    text = f"{SENTENCE}\n\tindented and then some more words to wrap here"
    processor = ComposerWrapProcessor()
    document = Document(text)
    for lineno, line in enumerate(document.lines):
        transformation = processor.apply_transformation(_input(document, lineno, line, 30))
        to_display = transformation.source_to_display
        to_source = transformation.display_to_source
        displayed = [to_display(i) for i in range(len(line) + 1)]
        assert displayed == sorted(displayed)
        assert [to_source(d) for d in displayed] == list(range(len(line) + 1))


def test_composer_click_past_a_rows_end_stays_on_that_row():
    line = "alpha beta gamma delta epsilon zeta eta theta"
    transformation = ComposerWrapProcessor().apply_transformation(
        _input(Document(line), 0, line, 20)
    )
    shown = "".join(text for _style, text, *_ in transformation.fragments)
    first_row = shown[:20]
    assert first_row.rstrip() == "> alpha beta gamma"
    # The padding after "gamma" maps to the space after it: the cursor lands
    # at the end of the row the click was on, not at the start of the next.
    assert transformation.display_to_source(19) == len("alpha beta gamma")


def _input(document: Document, lineno: int, line: str, width: int):
    from prompt_toolkit.layout.processors import TransformationInput

    return TransformationInput(
        buffer_control=None,
        document=document,
        lineno=lineno,
        source_to_display=lambda i: i,
        fragments=[("", line)],
        width=width,
        height=10,
    )


# ------------------------------------------------------------------ navigation


def test_up_and_down_walk_the_visual_rows_of_one_line():
    text = " ".join(["word"] * 20)  # 99 characters; rows of 75 text cells
    first_row_end = 75

    up = vertical_move(text, len(text), 80, -1)
    assert up == (len(text) - first_row_end, len(text) - first_row_end)

    down = vertical_move(text, 3, 80, 1)
    assert down == (first_row_end + 3, 3)
    # Nothing above the first row or below the last: the caller keeps the default.
    assert vertical_move(text, 3, 80, -1) is None
    assert vertical_move(text, len(text), 80, 1) is None


def test_vertical_moves_keep_the_goal_column_through_a_short_row():
    text = (
        "a long first line that wraps onto a second row here\n"
        "short\n"
        "another line that is long enough"
    )
    width = 32  # rows hold 29 cells of text
    start = text.index("wraps") + 2  # column 25 of the first row
    moved = vertical_move(text, start, width, 1)
    assert moved == (text.index("\nshort"), 25)  # the second row is shorter
    into_short, goal = vertical_move(text, moved[0], width, 1, goal=moved[1])
    assert (into_short, goal) == (text.index("\nanother"), 25)  # end of "short"
    beyond, _goal = vertical_move(text, into_short, width, 1, goal=goal)
    assert beyond - text.index("another") == 25


def test_up_key_moves_between_wrapped_rows_in_the_real_tui():
    # 80 columns: each composer row holds 75 cells of text, so the 99-character
    # message wraps after its fifteenth word.
    text = " ".join(["word"] * 20)
    with create_pipe_input() as pipe:
        pipe.send_text(text + "\x1b[A" + "X" + "\r" + "/exit\r")
        _result, transcript = app_module.run_tui(
            TuiState(model_name="m", username="u"),
            owl_color=False,
            input=pipe,
            output=_sized_output(80, 30),
        )
    sent = [body for role, body in transcript if role == "user"]
    assert sent == [text[:24] + "X" + text[24:]]


def test_down_key_moves_between_wrapped_rows_in_the_real_tui():
    text = " ".join(["word"] * 20)
    with create_pipe_input() as pipe:
        # Ctrl+A goes to the start of the line, Down to the row below it.
        pipe.send_text(text + "\x01" + "\x1b[B" + "X" + "\r" + "/exit\r")
        _result, transcript = app_module.run_tui(
            TuiState(model_name="m", username="u"),
            owl_color=False,
            input=pipe,
            output=_sized_output(80, 30),
        )
    sent = [body for role, body in transcript if role == "user"]
    assert sent == [text[:75] + "X" + text[75:]]


# ------------------------------------------------------------ real TUI screen


def _screen(columns: int, rows: int, *, text: str = "") -> list[str]:
    """The real TUI, drawn once at ``columns`` x ``rows`` with ``text`` typed."""
    captured: dict = {}
    real_run = Application.run
    Application.run = lambda self, *a, **k: captured.__setitem__("app", self)
    try:
        with create_pipe_input() as pipe:
            app_module.run_tui(
                TuiState(model_name="m", username="u"),
                owl_color=False,
                input=pipe,
                output=_sized_output(columns, rows),
            )
    finally:
        Application.run = real_run
    app = captured["app"]
    if text:
        with set_app(app):
            # Set, not typed: typing would start the slash completer, which
            # needs the event loop of a running app.
            app.layout.current_buffer.document = Document(text, len(text))
    return _paint(app, columns, rows)[0]


def _composer_rows(screen: list[str]) -> list[str]:
    top = next(i for i, row in enumerate(screen) if row.startswith("┌"))
    bottom = next(i for i, row in enumerate(screen) if i > top and row.startswith("└"))
    return [row[1:-1].rstrip() for row in screen[top + 1 : bottom]]


def test_real_composer_word_wraps_in_a_narrow_window():
    rows = _composer_rows(_screen(60, 30, text=SENTENCE))

    assert rows[0].startswith("> Can you please")
    assert all(row.startswith("  ") for row in rows[1:])
    assert " ".join(row[2:].strip() for row in rows).split() == SENTENCE.split()


def test_welcome_greeting_fits_or_yields_to_the_short_placeholder():
    wide = _composer_rows(_screen(120, 30))
    narrow = _composer_rows(_screen(60, 30))

    assert wide == ["> " + " " + INPUT_PLACEHOLDER]
    assert narrow == ["> " + " " + INPUT_PLACEHOLDER_FOLLOWUP]


def test_welcome_hint_reflows_instead_of_being_clipped():
    screen = _screen(44, 26)

    for phrase in HINT_TEXT.split(WELCOME_SEPARATOR):
        assert any(phrase in row for row in screen), phrase


@pytest.mark.parametrize("columns,rows", [(36, 24), (70, 16)])
def test_welcome_leaves_the_owl_out_when_it_cannot_fit(columns, rows):
    screen = _screen(columns, rows)
    text = "\n".join(screen)

    assert "█" not in text
    assert HEADING_TEXT in text
    assert "/help for everything" in text


def test_welcome_keeps_the_owl_when_there_is_room():
    screen = _screen(120, 30)

    assert any("█" in row for row in screen)


# ---------------------------------------------------------------- welcome text


def _plain_rows(width: int, **kwargs) -> list[str]:
    return ["".join(text for _style, text in row) for row in welcome_text_rows(width, **kwargs)]


def test_welcome_text_is_one_row_each_at_full_width():
    assert _plain_rows(120) == [f"{HEADING_TEXT}{WELCOME_SEPARATOR}{CREDIT_TEXT}", "", HINT_TEXT]


@pytest.mark.parametrize("width", [20, 30, 44, 60, 79])
def test_welcome_text_breaks_only_between_whole_phrases(width):
    rows = _plain_rows(width, setup_hint="Set up model access: /login · /config for an API key")
    phrases = [
        *HINT_TEXT.split(WELCOME_SEPARATOR),
        HEADING_TEXT,
        CREDIT_TEXT,
        "Set up model access: /login",
        "/config for an API key",
    ]

    assert all(len(row) <= width for row in rows)
    for phrase in phrases:
        if len(phrase) <= width:
            assert any(phrase in row for row in rows), (phrase, rows)
