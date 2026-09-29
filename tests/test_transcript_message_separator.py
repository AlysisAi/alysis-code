"""Consecutive assistant messages glued with no separator.

A closing message that streams into the same open block as the summary before
it produced "…(exit code 5)The --dry-run implementation is complete…". The
final text marks the exact boundary, so finish_assistant restores a paragraph
break there — while the provider-retry restream snap (large final over a small
abandoned prefix) keeps its existing behavior.
"""

from __future__ import annotations

from alysis_code.cli_impl.tui.transcript import TuiTranscript


def _entries(transcript: TuiTranscript) -> list[tuple[str, str]]:
    entries, _status, _streaming = transcript.snapshot()
    return list(entries)


def test_glued_closing_message_gets_paragraph_break() -> None:
    # Mirrors the live specimen: a long validation summary (the prefix is well
    # over 4x the closing line, so the retry-snap heuristic does not apply)
    # followed by a short closing message in the same open block.
    transcript = TuiTranscript()
    summary = (
        "Implemented `--dry-run`.\n\n"
        "- `script.py`: Added `argparse`; normal output remains `hi`, dry-run "
        "outputs `Would print: hi`.\n"
        "- `README.md`: Documented usage and behavior.\n\n"
        "Validation:\n\n"
        "- Normal invocation: passed\n"
        "- Dry-run invocation: passed\n"
        "- `ruff check .`: passed\n"
        "- `pytest -q`: no tests collected (exit code 5)"
    )
    closing = "The `--dry-run` implementation is complete and verified."
    transcript.stream_assistant(summary)
    transcript.stream_assistant(closing)  # second message, same open block

    transcript.finish_assistant(closing)

    entries = _entries(transcript)
    assert len(entries) == 1
    role, text = entries[0]
    assert role == "assistant"
    assert text == f"{summary}\n\n{closing}"


def test_retry_restream_snap_still_replaces_abandoned_prefix() -> None:
    transcript = TuiTranscript()
    abandoned = "The fix is"
    final = "The fix is complete and the suite passes on every platform we ship."
    transcript.stream_assistant(abandoned)
    transcript.stream_assistant(final)  # retry restreams the full reply

    transcript.finish_assistant(final)

    entries = _entries(transcript)
    assert len(entries) == 1
    assert entries[0][1] == final  # snapped, not separator-joined


def test_exact_final_is_untouched() -> None:
    transcript = TuiTranscript()
    text = "All done."
    transcript.stream_assistant(text)

    transcript.finish_assistant(text)

    entries = _entries(transcript)
    assert len(entries) == 1
    assert entries[0][1] == text


def test_palette_ellipsis_cuts_at_word_boundary() -> None:
    # The palette-ellipsis fix lives in the slash completer, but it is a one-liner to pin
    # here alongside the other display-text fixes.
    from alysis_code.cli_impl.chat_slash_completer import _ellipsize

    text = "Switch persona: code, architect, analyst, reviewer"
    out = _ellipsize(text, 36)
    assert out.endswith("...")
    body = out[:-3]
    # The visible text must end on a complete word from the source, never a
    # mid-word fragment like "architect, a".
    last_word = body.split()[-1].rstrip(",;:")
    assert last_word in {word.rstrip(",;:") for word in text.split()}


def test_palette_ellipsis_short_text_unchanged() -> None:
    from alysis_code.cli_impl.chat_slash_completer import _ellipsize

    assert _ellipsize("Show session status", 40) == "Show session status"


def test_whitespace_boundary_tail_fragment_stays_untouched() -> None:
    # The discriminator's other side (pins test_tui_duplicate_answer's case):
    # a final that merely restates the streamed ending across a natural
    # whitespace boundary is NOT a glued second message - no break inserted.
    transcript = TuiTranscript()
    long_reply = "Step one does A. Step two does B. Step three does C. Finally, run the tests."
    transcript.stream_assistant(long_reply)

    transcript.finish_assistant("run the tests.")

    entries = _entries(transcript)
    assert len(entries) == 1
    assert entries[0][1] == long_reply
