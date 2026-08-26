"""Console construction helpers for terminal-aware Rich output."""

from __future__ import annotations

import io
import os
import re
import sys
from typing import Any

from rich.console import Console as RichTerminal

_UNICODE_PROBE = "│╭╰─•·…⠋🟢📌"
_ENCODING_HINT = "Tip: enable UTF-8 mode for richer terminal rendering."
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|bearer)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(token)(\s*[:=]\s*)([^\s,;]+)"),
]
_ASCII_TRANSLATION = str.maketrans(
    {
        "│": "|",
        "┃": "|",
        "║": "|",
        "╭": "+",
        "╮": "+",
        "╰": "+",
        "╯": "+",
        "┌": "+",
        "┐": "+",
        "└": "+",
        "┘": "+",
        "├": "+",
        "┤": "+",
        "┬": "+",
        "┴": "+",
        "┼": "+",
        "─": "-",
        "━": "-",
        "═": "-",
        "╞": "+",
        "╡": "+",
        "╪": "+",
        "╟": "+",
        "╢": "+",
        "╫": "+",
        "•": "*",
        "·": "-",
        "…": "...",
        "→": "->",
        "←": "<-",
        "↔": "<->",
        "✓": "OK",
        "✔": "OK",
        "✗": "X",
        "✘": "X",
        "⠋": "*",
        "⠙": "*",
        "⠹": "*",
        "⠸": "*",
        "⠼": "*",
        "⠴": "*",
        "⠦": "*",
        "⠧": "*",
        "⠇": "*",
        "⠏": "*",
        "🟢": "[ready]",
        "🟡": "[pending]",
        "🔴": "[failed]",
        "⚪": "[minimal]",
        "📌": "[pinned]",
    }
)


def _stream_is_tty(stream: Any | None) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def _stream_encoding(stream: Any | None) -> str:
    encoding = getattr(stream, "encoding", None) or ""
    return encoding or "utf-8"


def _stream_can_encode(stream: Any | None, text: str) -> bool:
    encoding = _stream_encoding(stream)
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def stream_needs_ascii_fallback(stream: Any | None) -> bool:
    """Return true when Alysis Code should avoid Unicode terminal chrome."""
    return not _stream_can_encode(stream, _UNICODE_PROBE)


def redact_console_text(text: str) -> str:
    redacted = str(text)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def ascii_sanitize(text: str, *, stream: Any | None = None) -> str:
    """Return text that can be written to the target stream without Unicode errors."""
    clean = redact_console_text(str(text)).translate(_ASCII_TRANSLATION)
    if stream_needs_ascii_fallback(stream):
        return clean.encode("ascii", errors="replace").decode("ascii")
    encoding = _stream_encoding(stream)
    try:
        clean.encode(encoding)
        return clean
    except (LookupError, UnicodeEncodeError):
        try:
            return clean.encode(encoding, errors="replace").decode(encoding, errors="replace")
        except Exception:
            return clean.encode("ascii", errors="replace").decode("ascii")


def safe_write(stream: Any | None, text: str) -> None:
    """Write text without allowing terminal encoding failures to escape."""
    target = stream if stream is not None else sys.stdout
    safe_text = ascii_sanitize(text, stream=target)
    try:
        target.write(safe_text)
    except UnicodeError:
        try:
            target.write(safe_text.encode("ascii", errors="replace").decode("ascii"))
        except UnicodeError:
            return
    flush = getattr(target, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception:
            pass


def safe_plain_error(
    *,
    stream: Any | None = None,
    error_type: str = "Error",
    message: str = "",
    hint: str | None = None,
) -> None:
    """Emit a final plain ASCII error without invoking Rich rendering."""
    target = stream if stream is not None else sys.stderr
    clean_type = ascii_sanitize(error_type or "Error", stream=target).strip() or "Error"
    clean_message = ascii_sanitize(message or "No additional details.", stream=target).strip()
    lines = [f"Alysis Code {clean_type}: {clean_message}"]
    if hint:
        lines.append(ascii_sanitize(hint, stream=target).strip())
    elif stream_needs_ascii_fallback(target):
        lines.append(_ENCODING_HINT)
    safe_write(target, "\n".join(line for line in lines if line) + "\n")


def console_uses_ascii(console: Any) -> bool:
    value = getattr(console, "_alysis_ascii_only", None)
    if isinstance(value, bool):
        return value
    stream = getattr(console, "file", None)
    return stream_needs_ascii_fallback(stream)


def console_glyph(console: Any, unicode: str, ascii: str) -> str:
    return ascii if console_uses_ascii(console) else unicode


class EncodingSafeConsole(RichTerminal):
    """Rich console that degrades to ASCII instead of crashing on output encoding."""

    def __init__(self, *args: Any, ascii_only: bool | None = None, **kwargs: Any) -> None:
        stream = kwargs.get("file")
        if ascii_only is None:
            ascii_only = stream_needs_ascii_fallback(stream if stream is not None else sys.stdout)
        self._alysis_ascii_only = bool(ascii_only)
        kwargs.setdefault("safe_box", True)
        if self._alysis_ascii_only:
            kwargs.setdefault("legacy_windows", True)
            kwargs.setdefault("emoji", False)
        super().__init__(*args, **kwargs)

    def print(  # type: ignore[override]
        self,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
        style: Any | None = None,
        justify: Any | None = None,
        overflow: Any | None = None,
        no_wrap: bool | None = None,
        emoji: bool | None = None,
        markup: bool | None = None,
        highlight: bool | None = None,
        width: int | None = None,
        height: int | None = None,
        crop: bool = True,
        soft_wrap: bool | None = None,
        new_line_start: bool = False,
    ) -> None:
        if self._alysis_ascii_only:
            self._print_ascii_fallback(
                *objects,
                sep=sep,
                end=end,
                style=style,
                justify=justify,
                overflow=overflow,
                no_wrap=no_wrap,
                markup=markup,
                width=width,
                height=height,
                crop=crop,
                soft_wrap=soft_wrap,
                new_line_start=new_line_start,
            )
            return
        try:
            return super().print(
                *objects,
                sep=sep,
                end=end,
                style=style,
                justify=justify,
                overflow=overflow,
                no_wrap=no_wrap,
                emoji=False if self._alysis_ascii_only and emoji is None else emoji,
                markup=markup,
                highlight=highlight,
                width=width,
                height=height,
                crop=crop,
                soft_wrap=soft_wrap,
                new_line_start=new_line_start,
            )
        except UnicodeError:
            self._print_ascii_fallback(
                *objects,
                sep=sep,
                end=end,
                style=style,
                justify=justify,
                overflow=overflow,
                no_wrap=no_wrap,
                markup=markup,
                width=width,
                height=height,
                crop=crop,
                soft_wrap=soft_wrap,
                new_line_start=new_line_start,
            )

    def rule(  # type: ignore[override]
        self,
        title: Any = "",
        *,
        characters: str = "─",
        style: Any = "rule.line",
        align: str = "center",
    ) -> None:
        if self._alysis_ascii_only and characters == "─":
            characters = "-"
        try:
            return super().rule(title, characters=characters, style=style, align=align)
        except UnicodeError:
            text = str(title or "")
            width = max(int(getattr(self, "width", 80) or 80), 1)
            line = "-" * width if not text else f"{text} ".ljust(width, "-")
            safe_write(self.file, line + "\n")

    def _print_ascii_fallback(
        self,
        *objects: Any,
        sep: str,
        end: str,
        style: Any | None,
        justify: Any | None,
        overflow: Any | None,
        no_wrap: bool | None,
        markup: bool | None,
        width: int | None,
        height: int | None,
        crop: bool,
        soft_wrap: bool | None,
        new_line_start: bool,
    ) -> None:
        buffer = io.StringIO()
        fallback = RichTerminal(
            file=buffer,
            force_terminal=False,
            no_color=True,
            color_system=None,
            safe_box=True,
            legacy_windows=True,
            emoji=False,
            width=width or int(getattr(self, "width", 80) or 80),
        )
        try:
            fallback.print(
                *objects,
                sep=sep,
                end=end,
                style=style,
                justify=justify,
                overflow=overflow,
                no_wrap=no_wrap,
                emoji=False,
                markup=markup,
                highlight=False,
                width=width,
                height=height,
                crop=crop,
                soft_wrap=soft_wrap,
                new_line_start=new_line_start,
            )
            rendered = buffer.getvalue()
        except Exception:
            rendered = sep.join(str(obj) for obj in objects) + end
        safe_write(self.file, rendered)


def make_console(
    *,
    force_terminal: bool | None = None,
    no_color: bool | None = None,
    file: Any | None = None,
    **kwargs: Any,
) -> RichTerminal:
    """Create a Rich console that respects host terminal color capability."""
    effective_no_color = bool(no_color) or bool(os.environ.get("NO_COLOR"))
    stream = file if file is not None else sys.stdout
    ascii_only = stream_needs_ascii_fallback(stream)
    if effective_no_color:
        return EncodingSafeConsole(
            file=file,
            force_terminal=force_terminal,
            no_color=True,
            ascii_only=ascii_only,
            **kwargs,
        )

    if os.environ.get("WT_SESSION"):
        kwargs.setdefault("color_system", "truecolor")

    if force_terminal is None and not _stream_is_tty(stream):
        return EncodingSafeConsole(
            file=file,
            force_terminal=False,
            no_color=True,
            ascii_only=ascii_only,
            **kwargs,
        )

    if force_terminal is None:
        return EncodingSafeConsole(file=file, ascii_only=ascii_only, **kwargs)
    return EncodingSafeConsole(
        file=file,
        force_terminal=force_terminal,
        ascii_only=ascii_only,
        **kwargs,
    )


__all__ = [
    "EncodingSafeConsole",
    "ascii_sanitize",
    "console_glyph",
    "console_uses_ascii",
    "make_console",
    "redact_console_text",
    "safe_plain_error",
    "safe_write",
    "stream_needs_ascii_fallback",
]
