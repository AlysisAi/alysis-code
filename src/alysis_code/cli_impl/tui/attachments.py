"""Composer attachments for the full-screen TUI.

Dropping a file on the terminal — or pasting its path — arrives as ordinary
text, so the raw path used to land in the input box and stay there. This module
turns those paths into compact chips the composer shows instead::

    > [Image #1]

and resolves them back into real turn inputs on submit: images ride the turn as
image parts (``run_turn(image_paths=...)``), while files and folders expand to
their absolute path so the agent's own tools can read them.

Kept free of prompt_toolkit so the detection rules and the registry are
unit-testable without constructing the application.
"""

from __future__ import annotations

import mimetypes
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

AttachmentKind = Literal["image", "file", "folder"]

# Chips are plain text in the buffer (like paste tokens) so a mangled edit makes
# the token stop matching and drops the attachment, rather than the UI guessing
# a half-deleted reference back into existence.
ATTACHMENT_TOKEN_RE = re.compile(r"\[(?P<kind>Image|File|Folder) #(?P<id>[1-9]\d*)\]")

_KIND_TITLES: dict[AttachmentKind, str] = {
    "image": "Image",
    "file": "File",
    "folder": "Folder",
}

# The turn builder rejects anything bigger, so refuse at attach time with a
# readable message instead of failing the send.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# A single drop can carry a whole selection. Past this many paths the paste is
# far more likely to be a file listing than an attachment gesture.
MAX_DETECTED_PATHS = 10

# Nothing longer than this can be a list of paths, and a multi-megabyte paste
# must not pay for a scan that is going to fail anyway.
_MAX_DETECTED_CHARS = MAX_DETECTED_PATHS * 4096

# Extensions terminals hand over for images that ``mimetypes`` may not know
# about on a bare Windows install.
_EXTRA_IMAGE_SUFFIXES = frozenset({".webp", ".avif", ".heic", ".heif"})

_WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\)")
# POSIX shells escape these when a file manager drops a path into a terminal.
_POSIX_ESCAPE_RE = re.compile(r"\\([ \t()\[\]{}'\"`&;$!*?#~|<>\\])")

# Explorer drops a Windows path even when the terminal is running WSL, so
# "C:\Users\me\shot.png" has to be tried as "/mnt/c/Users/me/shot.png" too.
_WINDOWS_DRIVE_RE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$", re.DOTALL)
# Dragging out of Explorer's Linux view gives \\wsl$\Ubuntu\home\me\x (or
# \\wsl.localhost\...), which is just /home/me/x from inside that distro.
_WSL_UNC_RE = re.compile(
    r"^[\\/]{2}wsl(?:\$|\.localhost)[\\/][^\\/]+(?P<path>[\\/].*)$",
    re.IGNORECASE | re.DOTALL,
)
_DEFAULT_WSL_MOUNT_ROOT = "/mnt"


class AttachmentError(ValueError):
    """A path the composer cannot turn into an attachment chip."""


@dataclass(frozen=True)
class Attachment:
    """One chip in the composer, resolved to a real path on disk."""

    kind: AttachmentKind
    path: Path
    index: int
    size_bytes: int | None = None

    @property
    def token(self) -> str:
        return f"[{_KIND_TITLES[self.kind]} #{self.index}]"

    @property
    def label(self) -> str:
        return f"{_KIND_TITLES[self.kind].lower()} #{self.index}"

    def describe(self) -> str:
        """``image #1 · shot.png · 240 KB`` for the hint line under the input."""
        parts = [self.label, self.path.name or os.fspath(self.path)]
        if self.size_bytes is not None:
            parts.append(format_size(self.size_bytes))
        return " · ".join(parts)


def format_size(size_bytes: int) -> str:
    size = max(0, int(size_bytes))
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def looks_like_image(path: Path) -> bool:
    if path.suffix.lower() in _EXTRA_IMAGE_SUFFIXES:
        return True
    mime, _ = mimetypes.guess_type(path.name)
    return bool(mime and mime.startswith("image/"))


def normalize_dropped_path(raw: str) -> str | None:
    """Undo the quoting/escaping a terminal adds when a file is dropped on it.

    Handles ``"C:\\Users\\me\\a b.png"``, ``'/home/me/a b.png'``, POSIX
    backslash escapes (``/home/me/a\\ b.png``) and ``file://`` URIs. Returns
    ``None`` when nothing usable is left.
    """

    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    elif text.lower().startswith("file://"):
        parsed = urlparse(text)
        decoded = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
            decoded = f"//{parsed.netloc}{decoded}"
        # file:///C:/Users/... -> C:/Users/...
        if re.match(r"^/[A-Za-z]:", decoded):
            decoded = decoded[1:]
        text = decoded.strip()
    elif not _WINDOWS_PATH_RE.match(text):
        # Only POSIX-looking text can be unescaped — on Windows the backslash is
        # the separator, not an escape.
        text = _POSIX_ESCAPE_RE.sub(r"\1", text)
    return text or None


def _split_quoted(text: str) -> list[str]:
    """Split a single line on whitespace while keeping quoted runs together."""

    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


@lru_cache(maxsize=1)
def running_under_wsl() -> bool:
    """True when this interpreter is a Linux process inside WSL."""

    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    if os.name != "posix":
        return False
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    lowered = release.lower()
    return "microsoft" in lowered or "wsl" in lowered


@lru_cache(maxsize=1)
def wsl_mount_root() -> str:
    """Where WSL mounts the Windows drives (``/mnt`` unless wsl.conf says else)."""

    try:
        config = Path("/etc/wsl.conf").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _DEFAULT_WSL_MOUNT_ROOT
    section = ""
    for raw_line in config.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "automount" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().lower() == "root":
            root = value.strip().strip('"').rstrip("/")
            return root or _DEFAULT_WSL_MOUNT_ROOT
    return _DEFAULT_WSL_MOUNT_ROOT


def translate_windows_path(text: str, *, mount_root: str = _DEFAULT_WSL_MOUNT_ROOT) -> str | None:
    """Rewrite a Windows path into the form WSL can open, or ``None``.

    Explorer hands a terminal the Windows path no matter what is running in it,
    so a drop into a WSL session arrives as ``C:\\Users\\me\\shot.png`` — a path
    that does not exist from inside the distro. ``\\\\wsl$\\<distro>\\…`` (dragged
    out of Explorer's Linux view) is the same file the distro already sees.
    """

    candidate = str(text or "").strip()
    if not candidate:
        return None
    unc = _WSL_UNC_RE.match(candidate)
    if unc is not None:
        return unc.group("path").replace("\\", "/") or None
    drive = _WINDOWS_DRIVE_RE.match(candidate)
    if drive is None:
        return None
    rest = drive.group("rest").replace("\\", "/")
    root = (mount_root or _DEFAULT_WSL_MOUNT_ROOT).rstrip("/")
    return f"{root}/{drive.group('drive').lower()}/{rest}"


def _resolve_existing(candidate: str) -> Path | None:
    """Resolve a dropped candidate, requiring an explicit, existing location.

    Bare relative words are rejected on purpose: pasting ``src`` inside a repo
    that happens to have a ``src/`` directory must stay plain text, not silently
    become a folder attachment. A drag/drop always yields an absolute path (or a
    ``~``/``file://`` form), so nothing real is lost.
    """

    normalized = normalize_dropped_path(candidate)
    if not normalized:
        return None
    for text in _location_candidates(normalized):
        path = Path(text).expanduser()
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.exists():
            return resolved
    return None


def _location_candidates(normalized: str) -> list[str]:
    """The path as written, then its WSL form when that is what we are in."""

    candidates = [normalized]
    if running_under_wsl():
        translated = translate_windows_path(normalized, mount_root=wsl_mount_root())
        if translated and translated != normalized:
            candidates.append(translated)
    return candidates


def detect_dropped_paths(payload: str) -> list[Path]:
    """Return the paths ``payload`` consists of, or ``[]`` when it is prose.

    The whole payload must be nothing but existing paths. A mix of words and a
    path stays an ordinary paste, so typing about a file never turns into an
    attachment behind the user's back.
    """

    text = str(payload or "").strip()
    if not text or len(text) > _MAX_DETECTED_CHARS:
        return []

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []

    if len(lines) > 1:
        if len(lines) > MAX_DETECTED_PATHS:
            return []
        return _resolve_all(lines)

    single = lines[0]
    # One unquoted path with spaces ("/home/me/my shot.png") only resolves when
    # the whole line is tried as one path, so try that before splitting.
    whole = _resolve_existing(single)
    if whole is not None:
        return [whole]

    tokens = _split_quoted(single)
    if len(tokens) < 2 or len(tokens) > MAX_DETECTED_PATHS:
        return []
    return _resolve_all(tokens)


def _resolve_all(candidates: list[str]) -> list[Path]:
    """All-or-nothing: one unresolvable candidate makes the paste ordinary text."""

    resolved: list[Path] = []
    for candidate in candidates:
        found = _resolve_existing(candidate)
        if found is None:
            return []
        resolved.append(found)
    return resolved


def trailing_drop_run(text: str) -> str | None:
    """Return the just-completed dropped path at the end of ``text``.

    Not every terminal wraps a drop in bracketed-paste markers; some type the
    path into the application one character at a time, which never reaches the
    paste handler. This reads the tail of the composer instead — but only for
    the two shapes a terminal produces and a person typing a message does not:
    a fully quoted run, or a ``file://`` URI. Both have an unambiguous final
    character, so a path is never chipped halfway through being written.
    """

    tail = str(text or "")
    if not tail or tail[-1].isspace():
        return None

    if tail[-1] in {'"', "'"}:
        quote = tail[-1]
        opening = tail.rfind(quote, 0, len(tail) - 1)
        if opening == -1:
            return None
        run = tail[opening:]
        # A quoted word with no separator in it is just a quoted word.
        return run if ("/" in run or "\\" in run) else None

    token = tail.rsplit(None, 1)[-1] if tail.split() else ""
    if token.lower().startswith("file://"):
        return token
    return None


def classify_attachment(path: Path, *, max_image_bytes: int = MAX_IMAGE_BYTES) -> AttachmentKind:
    """Decide which chip ``path`` gets, refusing what a turn could not carry."""

    if path.is_dir():
        return "folder"
    if not path.is_file():
        raise AttachmentError(f"Not a file: {path}")
    if not looks_like_image(path):
        return "file"
    try:
        size = path.stat().st_size
    except OSError as exc:  # noqa: BLE001 - reported to the user, never fatal
        raise AttachmentError(f"Could not read {path.name}: {exc}") from exc
    if size > max_image_bytes:
        raise AttachmentError(
            f"{path.name} is {format_size(size)} - images must be "
            f"{format_size(max_image_bytes)} or smaller."
        )
    return "image"


def quote_path_for_instruction(path: Path) -> str:
    text = os.fspath(path)
    return f'"{text}"' if any(char.isspace() for char in text) else text


class AttachmentRegistry:
    """The chips currently live in the composer, keyed by their token text.

    Numbering runs per kind and never rewinds inside a session, mirroring the
    paste registry: a chip the user deleted does not renumber the others.
    """

    def __init__(self, *, max_image_bytes: int = MAX_IMAGE_BYTES) -> None:
        self._entries: dict[str, Attachment] = {}
        self._counts: dict[str, int] = {"image": 0, "file": 0, "folder": 0}
        self._max_image_bytes = int(max_image_bytes)

    def add_path(self, path: Path | str, *, root: Path | None = None) -> Attachment:
        """Register ``path`` and return its chip. Raises :class:`AttachmentError`.

        A relative ``path`` (only reachable from the typed ``/image <path>``
        form) is taken against ``root`` — the session's active workdir — rather
        than whatever the process happens to have as its CWD. A string goes
        through the same unquoting a drop does, so ``/image "C:\\my files\\a.png"``
        works the way the shell taught people to write it.
        """

        if isinstance(path, Path):
            text = os.fspath(path)
        else:
            text = normalize_dropped_path(str(path)) or str(path)
        # A typed "/image C:\Users\me\a.png" inside WSL deserves the same
        # translation a dropped path gets.
        for candidate in _location_candidates(text):
            probe = Path(candidate).expanduser()
            if probe.is_absolute() and probe.exists():
                text = candidate
                break
        resolved = Path(text).expanduser()
        if not resolved.is_absolute() and root is not None:
            resolved = Path(root) / resolved
        try:
            resolved = resolved.resolve()
        except OSError as exc:  # noqa: BLE001 - surfaced inline
            raise AttachmentError(f"Could not resolve {path}: {exc}") from exc
        if not resolved.exists():
            raise AttachmentError(f"File not found: {path}")
        kind = classify_attachment(resolved, max_image_bytes=self._max_image_bytes)
        size: int | None = None
        if kind != "folder":
            try:
                size = resolved.stat().st_size
            except OSError:
                size = None
        self._counts[kind] += 1
        attachment = Attachment(kind=kind, path=resolved, index=self._counts[kind], size_bytes=size)
        self._entries[attachment.token] = attachment
        return attachment

    def restore(self, attachments: Sequence[Attachment]) -> None:
        """Re-adopt chips that left the composer, keeping their original tokens.

        Used when a queued message is recalled back into the input box: the text
        still reads ``[Image #1]``, so that chip has to mean the same file it
        did when the message was queued.
        """

        for attachment in attachments:
            self._entries[attachment.token] = attachment
            self._counts[attachment.kind] = max(
                self._counts.get(attachment.kind, 0), attachment.index
            )

    def get(self, token: str) -> Attachment | None:
        return self._entries.get(token)

    def snapshot(self) -> dict[str, Attachment]:
        return dict(self._entries)

    def live_matches(self, text: str) -> list[re.Match[str]]:
        return [
            match
            for match in ATTACHMENT_TOKEN_RE.finditer(str(text or ""))
            if match.group(0) in self._entries
        ]

    def is_live_token(self, token: str) -> bool:
        return token in self._entries

    def retain_tokens(self, text: str) -> None:
        """Forget chips whose token no longer appears in ``text``."""

        live = {match.group(0) for match in ATTACHMENT_TOKEN_RE.finditer(str(text or ""))}
        for token in tuple(self._entries):
            if token not in live:
                self._entries.pop(token, None)

    def attachments_in(self, text: str) -> list[Attachment]:
        """Every registered chip in ``text``, in the order it is written."""

        return [self._entries[match.group(0)] for match in self.live_matches(text)]

    def image_paths(self, text: str) -> list[str]:
        return [
            os.fspath(attachment.path)
            for attachment in self.attachments_in(text)
            if attachment.kind == "image"
        ]

    def expand(self, text: str) -> str:
        """Turn chips into what the model should read.

        Image chips stay verbatim — the picture itself rides the turn as an
        image part, and the turn builder appends its own attachment note — while
        file and folder chips become the absolute path so the agent's tools can
        open them.
        """

        source = str(text or "")
        self.retain_tokens(source)

        def replace(match: re.Match[str]) -> str:
            attachment = self._entries.get(match.group(0))
            if attachment is None or attachment.kind == "image":
                return match.group(0)
            return quote_path_for_instruction(attachment.path)

        return ATTACHMENT_TOKEN_RE.sub(replace, source)

    def hint(self, text: str) -> str:
        """One dim line under the composer naming what is attached."""

        attachments = self.attachments_in(text)
        if not attachments:
            return ""
        if len(attachments) == 1:
            return attachments[0].describe()
        counts: dict[AttachmentKind, int] = {}
        for attachment in attachments:
            counts[attachment.kind] = counts.get(attachment.kind, 0) + 1
        parts = [f"{count} {kind}" + ("" if count == 1 else "s") for kind, count in counts.items()]
        return f"{len(attachments)} attachments - " + ", ".join(parts)


def token_at(text: str, cursor_position: int) -> re.Match[str] | None:
    for match in ATTACHMENT_TOKEN_RE.finditer(str(text or "")):
        if match.start() <= cursor_position <= match.end():
            return match
    return None


def token_before(text: str, cursor_position: int) -> re.Match[str] | None:
    for match in ATTACHMENT_TOKEN_RE.finditer(str(text or "")):
        if match.end() == cursor_position:
            return match
    return None


def token_after(text: str, cursor_position: int) -> re.Match[str] | None:
    for match in ATTACHMENT_TOKEN_RE.finditer(str(text or "")):
        if match.start() == cursor_position:
            return match
    return None


__all__ = [
    "ATTACHMENT_TOKEN_RE",
    "MAX_DETECTED_PATHS",
    "MAX_IMAGE_BYTES",
    "Attachment",
    "AttachmentError",
    "AttachmentKind",
    "AttachmentRegistry",
    "classify_attachment",
    "detect_dropped_paths",
    "format_size",
    "looks_like_image",
    "normalize_dropped_path",
    "quote_path_for_instruction",
    "running_under_wsl",
    "token_after",
    "token_at",
    "token_before",
    "trailing_drop_run",
    "translate_windows_path",
    "wsl_mount_root",
]
