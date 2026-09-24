"""Markdown → prompt_toolkit fragment rows for the TUI transcript.

A *completed* assistant reply is rendered through Rich's ``Markdown`` (the very
renderer the classic CLI uses, so the two agree on how a reply looks) into ANSI,
then converted to prompt_toolkit ``(style, text)`` rows. Streaming/partial text
and plain prose skip this entirely and render as plain lines, so a half-open
code fence never flashes mid-stream and a one-line answer is not reflowed.

Links stay clickable: every markdown link, and every bare URL in prose or code,
reaches the rows as a fragment styled by :func:`.links.link_style`, which the
transcript opens on click.

Kept free of any agent imports (Rich + prompt_toolkit only) so it unit-tests in
isolation, and every public entry point is fail-safe: on any error it returns
``None`` and the caller falls back to plain rendering.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from io import StringIO
from typing import Any

from pygments.style import Style as PygmentsStyle
from pygments.token import Comment, Keyword, Name, Number, Operator, String

from ...surface.styles import TerminalTheme
from .links import find_urls, is_openable, link_style, strip_link

# One visual line: a list of ``(style, text)`` fragments.
Row = list[tuple[str, str]]

# Matches the classic CLI's heuristic (rich_surface._looks_like_markdown) so the
# TUI and the plain console agree on what counts as markdown worth rendering.
_NUMBERED = re.compile(r"\d+\.\s")
_FENCED_CODE_RE = re.compile(r"```[^\n]*\n(.*?)(?:\n```|$)", re.S)
_CODE_FALLBACK_STYLE = "class:markdown.code"


class _NeutralCodeStyle(PygmentsStyle):
    """Syntax colours that preserve the terminal's own background."""

    background_color = None
    highlight_color = None
    styles = {
        Comment: "italic ansibrightblack",
        Keyword: "bold ansimagenta",
        Name.Builtin: "ansicyan",
        Number: "ansicyan",
        Operator: "bold",
        String: "ansigreen",
    }


def _code_theme(theme: TerminalTheme) -> Any:
    if theme == "dark":
        return "monokai"
    if theme == "light":
        return "friendly"
    return _NeutralCodeStyle


# Rich wraps markdown links (e.g. "[apnews.com](https://…)") in an OSC 8 terminal
# hyperlink — ``ESC]8;id=<n>;<url>ESC\ <anchor> ESC]8;;ESC\`` — whenever
# legacy_windows is off, which _render_ansi deliberately sets so Rich emits ANSI
# rather than Win32 console calls. prompt_toolkit's ANSI parser does not
# understand OSC: it drops the ESC/ST control bytes but leaks the payload
# ("8;id=1234;https://…" and "8;;") as visible text in the transcript (the stray
# line users see after a web-search answer). The terminal could not act on the
# hyperlink anyway (the TUI owns the mouse), so each OSC 8 marker is rewritten
# into a zero-width ``\x01…\x02`` run, which the ANSI parser hands back as its
# own fragment. _annotate_links then moves the target onto the anchor text's
# style and drops the marker; every other OSC sequence is stripped.
_OSC8_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_OSC_SEQUENCE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_LINK_MARK = "alysis-link:"
_LINK_MARKER_RE = re.compile("\x01" + re.escape(_LINK_MARK) + "[^\x02]*\x02")
# The parser does not recognise a zero-width run that directly follows another
# (it reads the second ``\x01`` as text), so back-to-back markers — Rich closes
# one link segment and reopens the next — collapse into the last one, which is
# the link state they leave behind.
_LINK_MARKER_RUN_RE = re.compile(f"(?:{_LINK_MARKER_RE.pattern}){{2,}}")


def looks_like_markdown(text: str) -> bool:
    """Detect Markdown syntax or web URLs; preserve unmarked prose layout."""
    clean = str(text or "")
    if not clean.strip():
        return False
    if "```" in clean:
        return True
    has_multiple_lines = "\n" in clean
    for line in clean.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith(("#", ">", "-", "*")):
            return True
        if _NUMBERED.match(stripped):
            return True
        if has_multiple_lines and stripped.count("|") >= 2:
            return True
    # Use the Markdown parser for inline syntax, including non-English labels.
    from rich.markdown import Markdown

    return bool(find_urls(clean)) or any(
        child.type in {"link_open", "strong_open", "em_open", "s_open", "code_inline"}
        for token in Markdown(clean).parsed
        for child in token.children or ()
    )


def _split_on_urls(token_type: Any, token: Any, matches: list[Any]) -> list[Any]:
    """``token`` as plain pieces with each URL wrapped in link_open/link_close."""
    content = token.content

    def piece(text: str) -> Any:
        clone = token.copy()
        clone.content = text
        return clone

    pieces: list[Any] = []
    cursor = 0
    for match in matches:
        if match.start > cursor:
            pieces.append(piece(content[cursor : match.start]))
        opener = token_type("link_open", "a", 1)
        opener.attrSet("href", match.target)
        pieces.extend(
            [opener, piece(content[match.start : match.end]), token_type("link_close", "a", -1)]
        )
        cursor = match.end
    if cursor < len(content):
        pieces.append(piece(content[cursor:]))
    return pieces


def _linkify_tokens(tokens: Iterable[Any]) -> None:
    """Make bare URLs in markdown prose and code spans real links, in place.

    Rich only hyperlinks explicit ``[text](url)`` / ``<url>`` links. Text already
    inside a link keeps its own target.
    """
    from markdown_it.token import Token

    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        children: list[Any] = []
        depth = 0
        for child in token.children:
            if child.type == "link_open":
                depth += 1
            elif child.type == "link_close":
                depth = max(0, depth - 1)
            elif depth == 0 and child.type in {"text", "code_inline"} and child.content:
                matches = find_urls(child.content)
                if matches:
                    children.extend(_split_on_urls(Token, child, matches))
                    continue
            children.append(child)
        token.children = children


@lru_cache(maxsize=1)
def _linked_markdown_type() -> Any:
    """Rich ``Markdown`` that also hyperlinks bare URLs in prose, code spans and code blocks."""
    from rich.markdown import CodeBlock, Markdown
    from rich.style import Style
    from rich.syntax import Syntax

    class _LinkedCodeBlock(CodeBlock):
        def __rich_console__(self, console: Any, options: Any) -> Any:
            for renderable in super().__rich_console__(console, options):
                if isinstance(renderable, Syntax):
                    # Rich expands tabs before applying stylized ranges, so the
                    # columns are measured on the expanded lines.
                    lines = renderable.code.expandtabs(renderable.tab_size).split("\n")
                    for number, line in enumerate(lines, start=1):
                        for match in find_urls(line):
                            renderable.stylize_range(
                                Style(link=match.target),
                                (number, match.start),
                                (number, match.end),
                            )
                yield renderable

    class _LinkedMarkdown(Markdown):
        elements = {**Markdown.elements, "fence": _LinkedCodeBlock, "code_block": _LinkedCodeBlock}

        def __init__(self, markup: str, **kwargs: Any) -> None:
            super().__init__(markup, **kwargs)
            _linkify_tokens(self.parsed)

    return _LinkedMarkdown


@lru_cache(maxsize=256)
def _render_ansi(text: str, width: int, theme: TerminalTheme) -> str:
    """Render markdown to ANSI at a fixed width and terminal theme.

    Completed replies never change, so a redraw reuses the cached ANSI; only a
    terminal resize or theme change forces a re-render. Hyperlinks come back as
    zero-width link markers (see ``_OSC8_RE``).
    """
    from rich.markdown import Markdown

    from ...surface.console import make_console

    def render(markdown_type: Any) -> str:
        buf = StringIO()
        console = make_console(
            file=buf,
            width=max(8, int(width)),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,  # no repr-highlighting of numbers/strings in prose
            emoji=False,  # ":)" etc. stay literal
            legacy_windows=False,  # emit ANSI escapes, not Win32 console calls
        )
        markdown = markdown_type(text, code_theme=_code_theme(theme))
        # Preserve explicit newlines in otherwise plain prose, including links.
        if all(
            token.type in {"paragraph_open", "inline", "paragraph_close"}
            for token in markdown.parsed
        ):
            for token in markdown.parsed:
                for child in token.children or ():
                    if child.type == "softbreak":
                        child.type = "hardbreak"
        console.print(markdown)
        return buf.getvalue()

    try:
        ansi = render(_linked_markdown_type())
    except Exception:
        # Finding bare URLs is an extra; it must never cost a reply its markdown.
        ansi = render(Markdown)
    return _mark_hyperlinks(ansi)


def _mark_hyperlinks(ansi: str) -> str:
    """Rewrite OSC 8 hyperlinks as zero-width link markers; strip every other OSC.

    CSI/SGR colour sequences are left untouched. A stray ``\\x01``/``\\x02`` in the
    rendered text is dropped first so it cannot open a zero-width run of its own.
    """
    clean = ansi.replace("\x01", "").replace("\x02", "")
    marked = _OSC8_RE.sub(lambda match: f"\x01{_LINK_MARK}{match.group(1)}\x02", clean)
    marked = _OSC_SEQUENCE_RE.sub("", marked)
    return _LINK_MARKER_RUN_RE.sub(
        lambda match: _LINK_MARKER_RE.findall(match.group(0))[-1],
        marked,
    )


def _annotate_links(fragments: Iterable[tuple[Any, ...]]) -> list[tuple[str, str]]:
    """Move each link marker's target onto the text it covers; drop the markers.

    Only an openable (``http``/``https``) target makes a link: a relative or
    other-scheme href is left as ordinary text.
    """
    annotated: list[tuple[str, str]] = []
    link = ""
    for style, text, *_rest in fragments:
        if "[ZeroWidthEscape]" in style:
            if text.startswith(_LINK_MARK):
                target = text[len(_LINK_MARK) :]
                link = link_style(target) if is_openable(target) else ""
            # Every zero-width run in this output is one of our markers; none
            # may reach the screen as a raw escape.
            continue
        annotated.append((f"{style} {link}".strip(), text) if link and text else (style, text))
    return annotated


def _is_blank(row: Row) -> bool:
    return not "".join(text for _style, text in row).strip()


def _trim_row_right(row: Row) -> Row:
    trimmed = list(row)
    while trimmed:
        style, text = trimmed[-1]
        right_stripped = text.rstrip()
        if right_stripped:
            if right_stripped != text:
                trimmed[-1] = (style, right_stripped)
            break
        trimmed.pop()
    return trimmed


def _split_row_to_width(row: Row, width: int) -> list[Row]:
    safe_width = max(1, int(width))
    rows: list[Row] = []
    current: Row = []
    current_len = 0
    for style, text in row:
        remaining = str(text)
        while remaining:
            available = safe_width - current_len
            if available <= 0:
                rows.append(_trim_row_right(current))
                current = []
                current_len = 0
                available = safe_width
            chunk = remaining[:available]
            current.append((style, chunk))
            current_len += len(chunk)
            remaining = remaining[available:]
    rows.append(_trim_row_right(current))
    return [item for item in rows if item]


def _fenced_code_line_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for match in _FENCED_CODE_RE.finditer(str(text or "")):
        for line in match.group(1).splitlines():
            stripped = line.strip()
            if stripped:
                keys.add(stripped)
    return keys


def _apply_code_fallback_styles(rows: list[Row], text: str) -> list[Row]:
    code_line_keys = _fenced_code_line_keys(text)
    if not code_line_keys:
        return rows
    styled_rows: list[Row] = []
    for row in rows:
        row_text = "".join(fragment_text for _style, fragment_text in row).strip()
        # A URL in the code is a link, not a colour: it must not exempt the row.
        if row_text in code_line_keys and not any(strip_link(style) for style, _text in row):
            styled_rows.append(
                [
                    (
                        f"{_CODE_FALLBACK_STYLE} {style}".strip() if fragment_text else style,
                        fragment_text,
                    )
                    for style, fragment_text in row
                ]
            )
            continue
        styled_rows.append(row)
    return styled_rows


@lru_cache(maxsize=256)
def render_markdown_rows(
    text: str,
    width: int,
    theme: TerminalTheme = "neutral",
) -> list[Row] | None:
    """Markdown-render ``text`` into rows of fragments, or ``None`` to render plain.

    Returns ``None`` when the text is not markdown (so the caller keeps its plain
    layout) or when rendering fails for any reason. Never raises. ``width`` is the
    target content width in columns.

    Memoized per ``(text, width, theme)`` — the transcript re-renders EVERY completed
    reply on every redraw, and a streaming turn redraws many times a second, so
    without this the full ANSI→fragments→wrap post-processing re-ran each frame
    for each finished reply (cost grows with conversation length → visible lag).
    Caching the ``_render_ansi`` string alone was not enough. The returned rows
    are shared across frames, so callers MUST treat them as read-only (both
    current callers build fresh lists rather than mutating in place).
    """
    if not looks_like_markdown(text):
        return None
    try:
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text
        from prompt_toolkit.formatted_text.utils import split_lines

        ansi = _render_ansi(text, int(width), theme)
        fragments = _annotate_links(to_formatted_text(ANSI(ansi.rstrip("\n"))))
        rows = []
        for line in split_lines(fragments):
            trimmed = _trim_row_right(list(line))
            if not trimmed:
                rows.append(trimmed)
                continue
            rows.extend(_split_row_to_width(trimmed, int(width)))
    except Exception:
        return None
    # Trim trailing blank rows Rich pads on (the leading ones, if any, are kept so
    # the caller can drop its accent marker on the first non-blank row).
    while rows and _is_blank(rows[-1]):
        rows.pop()
    rows = _apply_code_fallback_styles(rows, text)
    return rows or None


__all__ = ["render_markdown_rows", "looks_like_markdown", "Row"]
