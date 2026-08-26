"""Owl ASCII animation for the TUI.

This reuses the *exact* owl frames and loaders from the welcome screen
(``cli_impl/commands/welcome.py``) so the launch animation is identical to the
one the classic CLI shows. We only change how the frames are driven: instead of
raw ``time.sleep`` + cursor-up ANSI writes, the prompt_toolkit event loop
advances the frame index and repaints.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from prompt_toolkit.formatted_text import ANSI

# Render the owl as a crisp *white* silhouette on a dark terminal — without
# painting a background box. The source art is a single grayscale ramp (index 16
# plus the 232–255 gray ramp, with a stray system grey 7). The old mapping only
# lifted that ramp into 250–255, which left the owl reading as a washed-out light
# grey (#bcbcbc) and — worse — squashed the beak's faint mid-grey marks to within
# one step of the body, so the nose showed up as a smudge. Folding the whole ramp
# to pure white fixes both: the silhouette is genuinely white, and the beak/eyes
# now read cleanly because their shape is carried by the block-glyph geometry and
# the dark background showing through, not by tiny grey-on-grey contrast.
_OWL_WHITE = 231  # xterm-256 pure white (#ffffff)
_FG_256_RE = re.compile(r"\x1b\[38;5;(\d+)m")


def _whiten_index(idx: int) -> int:
    # Any grey the owl art uses → pure white; leave genuine colours untouched.
    if idx in (7, 8, 16) or 232 <= idx <= 255:
        return _OWL_WHITE
    return idx


def _whiten_frames(frames: list[list[str]]) -> list[list[str]]:
    def _sub(match: re.Match[str]) -> str:
        return f"\x1b[38;5;{_whiten_index(int(match.group(1)))}m"

    return [[_FG_256_RE.sub(_sub, line) for line in frame] for frame in frames]


def _load_frames(*, color_enabled: bool, stream: Any | None) -> list[list[str]]:
    """Load + crop the owl frames, returning [] when disabled or unavailable.

    Theme detection (which may probe the terminal via OSC 11) happens here, so
    callers must invoke this *before* the prompt_toolkit application takes over
    the terminal.
    """
    target_stream = stream if stream is not None else getattr(sys, "stdout", None)
    try:
        from ..commands.welcome import (  # local import avoids cli_surface cycles
            _crop_owl_logo_frames,
            _detect_owl_theme,
            _load_owl_logo_frames,
        )
    except Exception:
        return []
    try:
        theme = _detect_owl_theme(target_stream)
        frames = _load_owl_logo_frames(
            stream=target_stream,
            color_enabled=color_enabled,
            theme=theme,
        )
        frames = _crop_owl_logo_frames(frames)
        # White silhouette (no background box) on a confirmed dark terminal.
        # Light terminals keep the asset's dark grays; neutral frames were
        # stripped to the terminal's own foreground by the shared loader.
        if color_enabled and theme == "dark":
            frames = _whiten_frames(frames)
    except Exception:
        return []
    return [frame for frame in (frames or []) if frame]


class OwlAnimation:
    """Holds the owl frames and the current frame index."""

    def __init__(self, frames: list[list[str]]):
        self._frames = frames or []
        self._index = 0

    @property
    def available(self) -> bool:
        return bool(self._frames)

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    def advance(self) -> None:
        if self._frames:
            self._index = (self._index + 1) % len(self._frames)

    def current_ansi(self) -> ANSI | None:
        if not self._frames:
            return None
        frame = self._frames[self._index]
        return ANSI("\n".join(frame))


def load_owl_animation(*, color_enabled: bool = True, stream: Any | None = None) -> OwlAnimation:
    return OwlAnimation(_load_frames(color_enabled=color_enabled, stream=stream))


__all__ = ["OwlAnimation", "load_owl_animation"]
