"""Clickable links for the full-screen TUI.

The TUI runs with mouse support on, so the terminal hands every click to the
application and never gets to open a URL itself: a link that only *looks* like
one is dead. Links are therefore found and opened by the app.

A link travels through the row builders as an ordinary ``(style, text)``
fragment whose style carries the ``tui.transcript.link`` class (how it looks)
and a ``[link=…]`` token (where it goes). prompt_toolkit ignores bracketed style
tokens, so wrapping, selection highlighting and modal dimming all carry the
target along untouched, and a click reads it back from the fragment under the
pointer with :func:`link_at`.

Only ``http``/``https`` targets are ever produced: the text comes from model
output and fetched web pages, and handing an arbitrary scheme (``file:``,
``ms-msdt:``, …) to the OS opener would launch whatever handler it names.

Kept free of agent imports so it unit-tests in isolation.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote, urlsplit, urlunsplit

from prompt_toolkit.formatted_text.utils import fragment_list_width

# One visual line: a list of ``(style, text)`` fragments.
Row = list[tuple[str, str]]
# One wrapped chunk split into ``(text, target)`` runs; ``target`` is None off-link.
Segments = list[tuple[str, str | None]]

LINK_CLASS = "class:tui.transcript.link"
_TOKEN_PREFIX = "[link="

# A URL runs until whitespace, a control character, an angle bracket, a double
# quote or a backtick — none of which is valid unencoded in a URL, and all of
# which commonly delimit one in prose, markdown and code. A ``]`` ends it only
# when it opens a markdown link target (``[https://a](https://b)``), so IPv6
# hosts and ``?a[]=1`` queries survive.
_BODY = r"(?:[^\s<>\"`\]\x00-\x1f\x7f]|\](?!\())"
_SCHEME_URL = r"(?<![A-Za-z0-9])(?i:https?)://(?=[\w\[])" + _BODY + "+"
_WWW_URL = (
    r"(?<![\w.@/:-])(?i:www)\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"(?::\d{1,5})?(?:[/?#]" + _BODY + "*)?"
)
# Dev servers print ``localhost:5173`` as often as the full URL. Only the
# loopback/any-address hosts, and only with a port, so the bare word
# ``localhost`` in prose never becomes a link.
_LOCAL_URL = (
    r"(?<![\w.@/:-])(?:(?i:localhost)|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])"
    r":\d{1,5}(?![\w])(?:[/?#]" + _BODY + "*)?"
)
_URL_RE = re.compile(f"{_SCHEME_URL}|{_WWW_URL}|{_LOCAL_URL}")
_HAS_SCHEME_RE = re.compile(r"(?i)https?://")

# Characters a URL may contain but prose puts right after one: sentence
# punctuation, markdown emphasis, closing quotes (GitHub's autolink rule).
_TRAILING_PUNCTUATION = frozenset(".,:;!?*_~'\"")
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_UNICODE_PUNCTUATION = frozenset({"Pe", "Pf", "Pi", "Po", "Ps"})
# Tool traces and previews elide long values ("https://example.com/very/lo…");
# opening the visible prefix would load the wrong page.
_ELISIONS = ("…", "...")

# textwrap's own whitespace munging, so wrapped chunks stay exact substrings.
_WRAP_WHITESPACE = str.maketrans({ch: " " for ch in "\t\n\x0b\x0c\r"})
_ANY_ADDRESS_HOSTS = frozenset({"0.0.0.0", "::"})


@dataclass(frozen=True)
class UrlMatch:
    """A URL found in text: ``text[start:end]`` shows it, ``target`` opens it."""

    start: int
    end: int
    target: str


def is_openable(url: str) -> bool:
    """True for an absolute ``http``/``https`` URL safe to hand to a browser."""
    if not url or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def browser_url(url: str) -> str:
    """The address to actually open for ``url``.

    A dev server bound to every interface announces itself as
    ``http://0.0.0.0:8000``; browsers refuse the unspecified address, so it is
    opened as ``localhost`` on the same port instead. Anything else is unchanged.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return url
    if host not in _ANY_ADDRESS_HOSTS:
        return url
    netloc = f"localhost:{port}" if port else "localhost"
    return urlunsplit(parts._replace(netloc=netloc))


def _trim_trailing(raw: str) -> str:
    url = raw
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCTUATION:
            url = url[:-1]
            continue
        opener = _CLOSERS.get(last)
        if opener is not None:
            # Keep a closer the URL itself opened ("…/Foo_(bar)"); drop one that
            # belongs to the surrounding prose ("(see https://a.com)").
            if url.count(last) > url.count(opener):
                url = url[:-1]
                continue
            break
        if not last.isascii() and unicodedata.category(last) in _UNICODE_PUNCTUATION:
            url = url[:-1]
            continue
        break
    return url


def _may_hold_url(text: str) -> bool:
    """Cheap pre-check: False means ``text`` cannot contain a match of ``_URL_RE``.

    The transcript re-renders every row on every frame and almost no row holds a
    URL, so this stands in front of the regex.
    """
    if "://" in text:
        return True
    lowered = text.lower()
    # Spelled out rather than any(): this runs for every row on every frame.
    return (
        "www." in lowered
        or "localhost:" in lowered
        or "127.0.0.1:" in lowered
        or "0.0.0.0:" in lowered
        or "[::" in lowered
    )


def _target_for(shown: str) -> str | None:
    if _HAS_SCHEME_RE.match(shown):
        target = shown
    elif shown[:4].lower() == "www.":
        target = f"https://{shown}"
    else:
        target = f"http://{shown}"
    return target if is_openable(target) else None


def find_urls(text: str) -> list[UrlMatch]:
    """Every openable URL shown in ``text``, in order.

    Recognises ``http(s)://`` URLs, ``www.`` hosts and ``localhost:PORT``-style
    loopback addresses. Trailing sentence punctuation and unbalanced closing
    brackets are left out of the link, and an elided URL (``…`` / ``...``) is
    skipped rather than opened truncated.
    """
    if not text or not _may_hold_url(text):
        return []
    return list(_find_urls(text))


# Memoized: the transcript redraws the same rows on every frame.
@lru_cache(maxsize=4096)
def _find_urls(text: str) -> tuple[UrlMatch, ...]:
    found: list[UrlMatch] = []
    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        if "…" in raw:
            continue
        shown = _trim_trailing(raw)
        end = match.start() + len(shown)
        if not shown or text.startswith(_ELISIONS, end):
            continue
        target = _target_for(shown)
        if target is not None:
            found.append(UrlMatch(match.start(), end, target))
    return tuple(found)


def _encode_target(url: str) -> str:
    # Style strings split on whitespace (none survives is_openable) and
    # prompt_toolkit tests some tokens by substring ("[ZeroWidthEscape]",
    # "[transparent]"), so brackets are escaped; escaping "%" first keeps the
    # round trip exact.
    return url.replace("%", "%25").replace("[", "%5B").replace("]", "%5D")


@lru_cache(maxsize=4096)
def link_style(url: str, base: str = "") -> str:
    """``base`` restyled as a link to ``url``."""
    return f"{base} {LINK_CLASS} {_TOKEN_PREFIX}{_encode_target(url)}]".strip()


def link_target(style: str) -> str | None:
    """The URL a fragment style links to, or None."""
    if _TOKEN_PREFIX not in style:
        return None
    for part in reversed(style.split()):
        if part.startswith(_TOKEN_PREFIX) and part.endswith("]"):
            return unquote(part[len(_TOKEN_PREFIX) : -1]) or None
    return None


def strip_link(style: str) -> str:
    """``style`` without its link class and target."""
    if _TOKEN_PREFIX not in style and LINK_CLASS not in style:
        return style
    return " ".join(
        part for part in style.split() if part != LINK_CLASS and not part.startswith(_TOKEN_PREFIX)
    )


def link_at(row: Sequence[tuple[str, str]], column: int) -> str | None:
    """The URL under character ``column`` of ``row``, or None."""
    if column < 0:
        return None
    position = 0
    for style, text, *_rest in row:
        end = position + len(text)
        if position <= column < end:
            return link_target(style)
        position = end
    return None


def link_segments(line: str, chunks: Sequence[str]) -> list[Segments]:
    """Split each wrapped ``chunk`` of ``line`` into ``(text, target)`` runs.

    URLs are found in the whole logical line *before* wrapping, so a URL broken
    across rows still opens in full from any of its pieces. ``chunks`` must come
    from :mod:`textwrap` (or ``_wrap_line``) applied to ``line``: after textwrap's
    own whitespace munging each chunk is an exact, in-order substring. The
    returned segments are shared between calls and must be treated as read-only.
    """
    if not line or not _may_hold_url(line):
        return [[(chunk, None)] for chunk in chunks]
    return list(_link_segments(line, tuple(chunks)))


# Memoized: the transcript redraws the same rows on every frame.
@lru_cache(maxsize=4096)
def _link_segments(line: str, chunks: tuple[str, ...]) -> tuple[Segments, ...]:
    source = line.expandtabs().translate(_WRAP_WHITESPACE)
    matches = find_urls(source)
    if not matches:
        return tuple([(chunk, None)] for chunk in chunks)
    starts: list[int] = []
    cursor = 0
    for chunk in chunks:
        start = source.find(chunk, cursor)
        if start < 0:
            # Not a plain wrap of this line; linkify each chunk on its own.
            return tuple(_chunk_segments(chunk, find_urls(chunk), 0) for chunk in chunks)
        starts.append(start)
        cursor = start + len(chunk)
    return tuple(
        _chunk_segments(chunk, matches, start) for chunk, start in zip(chunks, starts, strict=True)
    )


def _chunk_segments(chunk: str, matches: Sequence[UrlMatch], offset: int) -> Segments:
    segments: Segments = []
    cursor = 0
    for match in matches:
        start = max(match.start - offset, 0)
        end = min(match.end - offset, len(chunk))
        if end <= start:
            continue
        if start > cursor:
            segments.append((chunk[cursor:start], None))
        segments.append((chunk[start:end], match.target))
        cursor = end
    if cursor < len(chunk) or not segments:
        segments.append((chunk[cursor:], None))
    return segments


def link_fragments(style: str, segments: Segments, *, prefix: str = "") -> Row:
    """Fragments for one wrapped chunk; exactly ``[(style, prefix + chunk)]`` off-link."""
    if len(segments) == 1 and segments[0][1] is None:
        return [(style, prefix + segments[0][0])]
    if not any(target for _text, target in segments):
        return [(style, prefix + "".join(text for text, _target in segments))]
    row: Row = [(style, prefix)] if prefix else []
    for text, target in segments:
        if text:
            row.append((link_style(target, style) if target else style, text))
    return row


def pad_after_trailing_link(row: Row, width: int) -> Row:
    """Give a row that ends in a link one blank cell after it, when it fits.

    prompt_toolkit maps a click right of a line's text onto that line's last
    character, so without a non-link cell after the URL, clicking the empty
    space beside it would open the link.
    """
    if not row or (row[-1][1] and _TOKEN_PREFIX not in row[-1][0]):
        return row  # the common case, decided without a scan
    last = next((style for style, text in reversed(row) if text), None)
    if last is None or _TOKEN_PREFIX not in last:
        return row
    if fragment_list_width(row) >= width:
        return row
    return [*row, ("", " ")]


__all__ = [
    "LINK_CLASS",
    "Row",
    "Segments",
    "UrlMatch",
    "browser_url",
    "find_urls",
    "is_openable",
    "link_at",
    "link_fragments",
    "link_segments",
    "link_style",
    "link_target",
    "pad_after_trailing_link",
    "strip_link",
]
