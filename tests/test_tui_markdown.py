"""Phase 3 TUI tests: markdown rendering of completed assistant replies.

A finished reply with block-level markdown (headings, lists, fenced code) is
rendered through Rich into styled rows; plain prose and still-streaming text are
left untouched so a half-open code fence never flickers mid-stream.
"""

from __future__ import annotations

from alysis_code.cli_impl.tui.app import _assistant_rows
from alysis_code.cli_impl.tui.markdown import (
    _render_ansi,
    looks_like_markdown,
    render_markdown_rows,
)
from alysis_code.cli_impl.tui.transcript import TuiTranscript

_CODE_REPLY = "Here you go:\n\n```python\ndef f(x):\n    return x + 1\n```"
_LIST_REPLY = "Steps:\n\n- first\n- second\n\n## Heading\n\nmore text"


def _row_text(row: list[tuple[str, str]]) -> str:
    return "".join(text for _style, text in row)


# ------------------------------ heuristic ------------------------------


def test_looks_like_markdown_detects_blocks():
    assert looks_like_markdown("# Title")
    assert looks_like_markdown("- a\n- b")
    assert looks_like_markdown("1. one\n2. two")
    assert looks_like_markdown("```\ncode\n```")
    assert looks_like_markdown("| a | b |\n| - | - |")


def test_looks_like_markdown_skips_plain_prose():
    assert not looks_like_markdown("")
    assert not looks_like_markdown("   ")
    assert not looks_like_markdown("just a single sentence with no markup")
    # Single newline-joined prose must stay plain (else markdown reflows it).
    assert not looks_like_markdown("Hello\nworld")


# --------------------------- render_markdown_rows ---------------------------


def test_render_returns_none_for_plain_text():
    assert render_markdown_rows("Hello\nworld", 80) is None
    assert render_markdown_rows("", 80) is None


def test_render_code_block_keeps_code_and_styles_it():
    rows = render_markdown_rows(_CODE_REPLY, 60)
    assert rows is not None
    joined = "\n".join(_row_text(r) for r in rows)
    assert "def f(x):" in joined
    assert "return x + 1" in joined
    # The fenced block is syntax-highlighted, so at least one fragment on the
    # code row carries a non-empty style even when the neutral palette inherits
    # the terminal background.
    code_row = next(r for r in rows if "def f(x):" in _row_text(r))
    assert any(style for style, _text in code_row), "code row should be styled"


def test_render_list_and_heading():
    rows = render_markdown_rows(_LIST_REPLY, 60)
    assert rows is not None
    joined = "\n".join(_row_text(r) for r in rows)
    assert "first" in joined and "second" in joined
    assert "Heading" in joined
    # Rich renders bullets as "•".
    assert "•" in joined


def test_render_never_raises_and_rows_fit_width():
    width = 40
    rows = render_markdown_rows(_LIST_REPLY, width)
    assert rows is not None
    for row in rows:
        assert len(_row_text(row)) <= width


def test_render_strips_leaked_osc8_hyperlink_payload():
    # A web-search answer that links a source. Rich turns the markdown link into an
    # OSC 8 terminal hyperlink; prompt_toolkit's ANSI parser can't consume OSC and
    # used to leak "8;id=…;https://…" and the closing "8;;" as visible text. The
    # anchor text must survive, but none of the escape payload may.
    reply = (
        "The 4 teams remaining are:\n\n"
        "- Argentina\n"
        "- Spain "
        "([apnews.com](https://apnews.com/article/d47ccb4ac5b3af67?utm_source=openai))\n"
    )
    rows = render_markdown_rows(reply, 58)
    assert rows is not None
    joined = "\n".join(_row_text(r) for r in rows)
    assert "apnews.com" in joined  # anchor text preserved
    assert "8;id=" not in joined  # OSC 8 open marker gone
    assert "8;;" not in joined  # OSC 8 close marker gone
    assert "https://apnews.com" not in joined  # raw URL payload not leaked
    assert "\x1b]" not in joined  # no stray OSC introducer


def test_render_markdown_rows_is_memoized():
    # The transcript re-renders every completed reply on every redraw, so the
    # result is cached per (text, width): a repeat call returns the SAME object
    # (the cache hit). Callers therefore MUST treat the rows as read-only.
    first = render_markdown_rows(_LIST_REPLY, 58)
    second = render_markdown_rows(_LIST_REPLY, 58)
    assert first is not None
    assert first is second  # shared cache entry, not a fresh render
    # A different width is a distinct cache entry (a real render, not the cached one).
    assert render_markdown_rows(_LIST_REPLY, 44) is not first


def test_markdown_render_cache_and_code_background_follow_theme(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    _render_ansi.cache_clear()
    render_markdown_rows.cache_clear()

    dark = render_markdown_rows(_CODE_REPLY, 58, "dark")
    light = render_markdown_rows(_CODE_REPLY, 58, "light")
    neutral = render_markdown_rows(_CODE_REPLY, 58, "neutral")

    assert dark is not None and light is not None and neutral is not None
    assert dark is not light and light is not neutral
    dark_styles = {style for row in dark for style, _text in row if style}
    light_styles = {style for row in light for style, _text in row if style}
    neutral_styles = {style for row in neutral for style, _text in row if style}
    assert any("bg:#272822" in style for style in dark_styles)
    assert any("bg:#f0f0f0" in style for style in light_styles)
    assert all("bg:" not in style for style in neutral_styles)


def test_doc_panel_copies_rows_so_it_cannot_corrupt_the_cache():
    # render_markdown_rows is memoized (shared objects), so a caller that aliases
    # its rows must copy them. The doc panel does; a later transcript render of the
    # same (text, width) must be unaffected even if the panel's rows are mutated.
    from alysis_code.cli_impl.tui.app import _render_doc_panel_rows

    panel_rows = _render_doc_panel_rows(_LIST_REPLY, 58)
    for row in panel_rows:  # hostile in-place mutation of the panel's own rows
        row.clear()
    # _assistant_rows(width=60) renders markdown at width 60-2 == 58, the same key.
    rows = _assistant_rows(_LIST_REPLY, width=60, markdown=True)
    joined = "\n".join(_row_text(r) for r in rows)
    assert "first" in joined and "Heading" in joined  # cache entry intact


# ----------------------------- _assistant_rows -----------------------------


def test_assistant_rows_markdown_puts_marker_on_first_visible_row():
    rows = _assistant_rows(_CODE_REPLY, width=60, markdown=True)
    # Exactly one row carries the accent marker prefix.
    marker_rows = [r for r in rows if _row_text(r).startswith("✦ ")]
    assert len(marker_rows) == 1
    assert _row_text(marker_rows[0]).startswith("✦ Here you go")
    # The code survives the marker/indent wrapping.
    joined = "\n".join(_row_text(r) for r in rows)
    assert "def f(x):" in joined


def test_assistant_rows_streaming_stays_plain():
    # A half-open fence is markdown-shaped but must NOT be rendered while
    # streaming — it should pass through as plain lines.
    partial = "Here you go:\n\n```python\ndef f(x):"
    rows = _assistant_rows(partial, width=60, markdown=False)
    joined = "\n".join(_row_text(r) for r in rows)
    assert "```python" in joined  # fence kept verbatim, not consumed by Rich
    assert rows[0][0][1] == "✦ "


def test_assistant_rows_plain_text_unchanged():
    # Regression: non-markdown text renders exactly as before (marker + indent).
    rows = _assistant_rows("Hello\nworld")
    assert _row_text(rows[0]).startswith("✦ Hello")
    assert _row_text(rows[1]) == "  world"


# ------------------------------- snapshot -------------------------------


def test_snapshot_exposes_streaming_index():
    t = TuiTranscript()
    t.append_user("hi")
    t.begin_turn()
    t.stream_assistant("partial")
    entries, _status, streaming_index = t.snapshot()
    assert entries[streaming_index] == ("assistant", "partial")
    t.finish_assistant("partial done")
    _entries, _status2, idx_after = t.snapshot()
    assert idx_after is None  # block closed → renders as markdown


# --------------------------- headless integration ---------------------------


class _MarkdownSession:
    """Fake agent session that streams a markdown reply (with a fenced code
    block) into the surface, the way a real turn would."""

    def __init__(self, surface) -> None:
        self.surface = surface

    def run_turn(self, text: str, *, cancellation_token=None, **_kwargs) -> int:
        self.surface.on_user_message(text)
        # Deltas concatenate to exactly _CODE_REPLY (finish_assistant keeps the
        # streamed content when present).
        for delta in (
            "Here you go:\n\n",
            "```python\n",
            "def f(x):\n",
            "    return x + 1\n",
            "```",
        ):
            self.surface.on_assistant_token(delta)
        self.surface.on_assistant_message_done(_CODE_REPLY)
        return 0

    def close(self) -> None:  # pragma: no cover - parity with real session
        pass


def test_headless_markdown_reply_renders_without_crashing():
    # Drives a full ``run_tui`` render loop (a markdown reply must survive the
    # transcript render path, not just ``_assistant_rows`` in isolation).
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    state = TuiState(model_name="deepseek-chat", username="t")
    # No command_runner: the first line runs a turn, "/exit" is an exit word that
    # tears the app down (a command_runner returning "run" would loop forever).
    with create_pipe_input() as pipe:
        pipe.send_text("hi\r/exit\r")
        _result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=_MarkdownSession,
            background_turns=False,
        )
    assert ("assistant", _CODE_REPLY) in transcript
