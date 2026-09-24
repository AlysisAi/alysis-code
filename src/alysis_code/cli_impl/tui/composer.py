"""Composer layout: word wrap with a hanging indent.

prompt_toolkit wraps an input line at whichever character reaches the right
edge. In a narrow window that splits words across rows (``… of this c`` /
``omposer …``) and starts every continuation at column 0, under the prompt.
The transcript already renders the same message word-wrapped under a two-column
hanging indent; this module gives the composer that layout::

    > Can you check how this message wraps
      when the window is narrow?

It cooperates with prompt_toolkit's wrapping instead of replacing it: the
processor pads every visual row it lays out to exactly the window width, so
prompt_toolkit's character wrap breaks exactly where the word wrap did, and its
own row arithmetic (the frame's height, scrolling to the cursor) agrees with
what is drawn. The buffer text never changes. The prompt, the indents and the
padding exist only on screen, and the position mappings keep the cursor, mouse
clicks and the completion menu on the right cell.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.utils import get_cwidth

COMPOSER_PROMPT = "> "
# A tab is drawn as this many blank cells (prompt_toolkit would print "^I").
TAB_CELLS = 4
# The widest cell group one character can be drawn as: a tab, or a C1 control
# shown as "<80>". A row narrower than that cannot be guaranteed to make
# progress, so a pane that tight falls back to prompt_toolkit's plain wrap.
_MIN_ROW_CELLS = 4


class ComposerRow(NamedTuple):
    """One visual row of a logical line: characters ``[start, end)`` in ``cells`` columns."""

    start: int
    end: int
    cells: int


def _drawn_as(char: str) -> tuple[str, str]:
    """How one character is drawn: ``(text, extra style)``.

    Control characters get the same substitution prompt_toolkit's renderer
    would make ("^A", "<80>"). Doing it here keeps the row arithmetic counting
    the cells that are really drawn — prompt_toolkit's own height estimate
    counts such characters as zero-width.
    """
    if char == "\t":
        return " " * TAB_CELLS, ""
    mapped = Char.display_mappings.get(char)
    if mapped is None:
        return char, ""
    return mapped, " class:nbsp" if char == "\xa0" else " class:control-character"


def _is_space(char: str) -> bool:
    return char == " " or char == "\t"


def _measure(
    chars: Sequence[str],
) -> tuple[list[tuple[str, str]], list[int], list[bool], list[bool]]:
    """Per character: how it is drawn, its cells, and its ``spaces``/``solos`` flags."""
    drawn = [_drawn_as(char) for char in chars]
    widths = [get_cwidth(text) for text, _extra_style in drawn]
    spaces = [_is_space(char) for char in chars]
    solos = [
        width > 1 and text == char
        for (text, _extra_style), width, char in zip(drawn, widths, chars, strict=True)
    ]
    return drawn, widths, spaces, solos


def _rows_needed(cells: int, cap: int) -> int:
    return -(-max(0, cells) // cap)


def wrap_cells(
    widths: Sequence[int],
    spaces: Sequence[bool],
    solos: Sequence[bool],
    *,
    first_cap: int,
    rest_cap: int,
) -> list[ComposerRow]:
    """Greedy word wrap of one logical line into visual rows.

    ``widths[i]`` is the cell width of character ``i``; ``spaces[i]`` marks a
    break opportunity; ``solos[i]`` marks a wide glyph (CJK, emoji) that may
    break on either side, since such text has no spaces to break at.
    ``first_cap``/``rest_cap`` are the text cells of the first and the
    continuation rows. Each row keeps one more column free on the right: the
    cursor needs it at the end of the text, and it is where the space that
    ends a row may hang, so the next row never starts with that space.

    A word longer than a whole row is broken by character. It moves to a
    fresh row first whenever starting there costs no extra row.
    """
    n = len(widths)
    rows: list[ComposerRow] = []
    start = used = i = 0
    cap = first_cap

    def break_before(index: int) -> None:
        nonlocal start, used, cap
        rows.append(ComposerRow(start, index, used))
        start, used, cap = index, 0, rest_cap

    while i < n:
        if spaces[i]:
            if used + widths[i] <= cap + 1 or not used:
                used += widths[i]
                i += 1
            else:
                break_before(i)
            continue
        end = i + 1
        if not solos[i]:
            while end < n and not spaces[end] and not solos[end]:
                end += 1
        word = sum(widths[i:end])
        if used + word <= cap:
            used += word
            i = end
            continue
        if used and _rows_needed(word, rest_cap) <= _rows_needed(word - (cap - used), rest_cap):
            break_before(i)
            continue
        while i < end:
            width = widths[i]
            # Zero-width marks stay on the row of the glyph they combine with.
            if width and used and used + width > cap:
                break_before(i)
            used += width
            i += 1
    rows.append(ComposerRow(start, n, used))
    if used > cap:
        # The line ends in a space that took the cursor's column: the cursor
        # moves to a row of its own, as it does in any editor.
        rows.append(ComposerRow(n, n, 0))
    return rows


def _caps(width: int, prompt_cells: int, indent: int) -> tuple[int, int] | None:
    first_cap = width - prompt_cells - 1
    rest_cap = width - indent - 1
    if min(first_cap, rest_cap) < _MIN_ROW_CELLS:
        return None
    return first_cap, rest_cap


class ComposerWrapProcessor(Processor):
    """Draw the composer's prompt and word-wrap each line under a hanging indent.

    Line 0 starts with ``prompt``; every other line and every continuation row
    starts with blanks of the same width, so all of the text lines up in one
    column, the way the transcript shows the sent message. While the buffer is
    empty the placeholder's continuation rows sit ``placeholder_indent`` cells
    further in, under its text rather than under the cursor cell it starts
    with.

    Must be the last processor: the padding it adds is sized for the width
    prompt_toolkit renders at, and nothing after it may change that width.
    """

    def __init__(
        self,
        prompt: str = COMPOSER_PROMPT,
        *,
        prompt_style: str = "class:tui.prompt",
        placeholder_indent: int = 1,
    ) -> None:
        self._prompt = prompt
        self._prompt_style = prompt_style
        self._prompt_cells = max(1, get_cwidth(prompt))
        self._placeholder_indent = placeholder_indent

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        lead = self._prompt_cells
        if ti.lineno == 0:
            head: tuple[str, str] = (self._prompt_style, self._prompt)
        else:
            head = ("", " " * lead)
        indent = lead + (0 if ti.document.text else self._placeholder_indent)
        caps = _caps(ti.width, lead, indent)
        if caps is None:
            return self._prompt_only(head, ti.fragments)

        chars = explode_text_fragments(ti.fragments)
        drawn, widths, spaces, solos = _measure([fragment[1] for fragment in chars])
        rows = wrap_cells(widths, spaces, solos, first_cap=caps[0], rest_cap=caps[1])

        n = len(chars)
        fragments: StyleAndTextTuples = []
        # Display index of each character (n: the end of the line), and the
        # source index each drawn cell maps back to.
        to_display = [0] * (n + 1)
        to_source: list[int] = []
        last = len(rows) - 1
        for number, row in enumerate(rows):
            style, prefix = head if number == 0 else ("", " " * indent)
            fragments.append((style, prefix))
            # A click on the prompt or an indent lands on the row's first character.
            to_source.extend([row.start] * len(prefix))
            for index in range(row.start, row.end):
                to_display[index] = len(to_source)
                fragment = chars[index]
                text, extra_style = drawn[index]
                fragments.append((fragment[0] + extra_style, text, *fragment[2:]))
                to_source.extend([index] * len(text))
            if number < last:
                pad = ti.width - get_cwidth(prefix) - row.cells
                if pad > 0:
                    fragments.append(("", " " * pad))
                    # A click past the end of a row stays on that row: the
                    # row's own end position is drawn at the start of the next.
                    to_source.extend([max(row.start, row.end - 1)] * pad)
        to_display[n] = len(to_source)
        total = len(to_source)

        def source_to_display(i: int) -> int:
            if 0 <= i <= n:
                return to_display[i]
            return i if i < 0 else to_display[n] + (i - n)

        def display_to_source(i: int) -> int:
            if 0 <= i < total:
                return to_source[i]
            return i if i < 0 else n + (i - total)

        return Transformation(fragments, source_to_display, display_to_source)

    @staticmethod
    def _prompt_only(head: tuple[str, str], fragments: StyleAndTextTuples) -> Transformation:
        shift = len(head[1])
        return Transformation(
            [head, *fragments],
            source_to_display=lambda i: i + shift,
            display_to_source=lambda i: max(0, i - shift),
        )


class _VisualRow(NamedTuple):
    start: int  # buffer index of the row's first character
    end: int  # buffer index one past its last character
    last: bool  # final row of its logical line: its end position is drawn here


def _visual_rows(
    text: str, width: int, prompt_cells: int
) -> tuple[list[_VisualRow], list[int]] | None:
    """Every visual row of a non-empty buffer, plus each character's cell width."""
    caps = _caps(width, prompt_cells, prompt_cells)
    if caps is None:
        return None
    rows: list[_VisualRow] = []
    widths: list[int] = []
    offset = 0
    for line in text.split("\n"):
        _drawn, line_widths, spaces, solos = _measure(line)
        line_rows = wrap_cells(line_widths, spaces, solos, first_cap=caps[0], rest_cap=caps[1])
        final = len(line_rows) - 1
        rows.extend(
            _VisualRow(offset + row.start, offset + row.end, number == final)
            for number, row in enumerate(line_rows)
        )
        widths.extend(line_widths)
        widths.append(0)  # the newline
        offset += len(line) + 1
    return rows, widths


def vertical_move(
    text: str,
    cursor: int,
    width: int,
    delta: int,
    *,
    goal: int | None = None,
    prompt_cells: int = len(COMPOSER_PROMPT),
) -> tuple[int, int] | None:
    """Move the cursor one visual row up (``delta=-1``) or down (``delta=1``).

    Returns ``(new_cursor, goal)``, where ``goal`` is the column the move aimed
    for. Pass it back on the next move so a pass through a short row doesn't
    lose the column. Returns ``None`` when there is no row that way (the caller
    keeps prompt_toolkit's default), or when the pane is too narrow to lay out.
    Every row's text starts at the same column, so columns are measured from
    there.
    """
    layout = _visual_rows(text, width, prompt_cells)
    if layout is None:
        return None
    rows, widths = layout
    current = next(
        (
            number
            for number, row in enumerate(rows)
            if row.start <= cursor < row.end or (cursor == row.end and row.last)
        ),
        None,
    )
    if current is None:
        return None
    target = current + delta
    if not 0 <= target < len(rows):
        return None
    here = rows[current]
    column = goal if goal is not None else sum(widths[here.start : cursor])
    row = rows[target]
    stop = row.end if row.last else max(row.start, row.end - 1)
    position, used = row.start, 0
    while position < stop and used + widths[position] <= column:
        used += widths[position]
        position += 1
    return position, column


__all__ = [
    "COMPOSER_PROMPT",
    "ComposerRow",
    "ComposerWrapProcessor",
    "TAB_CELLS",
    "vertical_move",
    "wrap_cells",
]
