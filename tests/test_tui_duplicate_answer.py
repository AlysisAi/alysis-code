"""Regression tests for the TUI rendering the same assistant answer 2-3 times.

A multi-step agent turn can re-emit the same answer across continuation / tool
steps. Each streamed answer builds its own ``("assistant", text)`` transcript
block, and once the live block was closed by an intervening tool/trace line the
final ``on_assistant_message_done`` used to append the whole reply AGAIN — so the
identical answer stacked up in the pane. Two layers now guard against it:

- ``TuiTranscript.finish_assistant`` no longer appends a fresh block that merely
  repeats the turn's most recent assistant block (so the duplicate never enters
  the model at all), and
- ``_duplicate_assistant_indices`` collapses any verbatim repeat at render time
  (the safety net for still-open streamed duplicates).

Both are content-based and provider-agnostic (no model-specific logic).
"""

from __future__ import annotations

from alysis_code.cli_impl.tui.app import _duplicate_assistant_indices
from alysis_code.cli_impl.tui.transcript import TuiTranscript


def _assistant_texts(t: TuiTranscript) -> list[str]:
    return [text for role, text in t.entries if role == "assistant"]


# ------------------------- finish_assistant idempotency -------------------------


def test_restart_assistant_clears_live_block_for_restream():
    # The live path: the client's retry recorder fires the stream_restart hook
    # BEFORE the retry restreams, so the abandoned tokens vanish immediately
    # instead of doubling until finish_assistant's collapse.
    gen_a = "Hi! Ready to help with your ci-gate repo. What would you like to work on?"
    gen_b = "Hi! Ready to help with the ci-gate repo. What would you like to work on?"
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant(gen_a)
    t.restart_assistant()
    assert _assistant_texts(t) == [""]
    t.stream_assistant(gen_b)
    t.finish_assistant(gen_b)
    assert _assistant_texts(t) == [gen_b]


def test_restart_assistant_without_open_block_is_a_noop():
    t = TuiTranscript()
    t.begin_turn()
    t.restart_assistant()
    assert _assistant_texts(t) == []


def test_provider_retry_restream_collapses_to_final_reply():
    # A mid-stream transport failure abandons generation A after its tokens
    # rendered; the provider retry re-runs the request and generation B streams
    # into the SAME open block. Wordings differ ("your" vs "the"), so the
    # verbatim dedupe can never catch it — finish_assistant must snap the
    # block to the authoritative final text.
    gen_a = "Hi! Ready to help with your ci-gate repo. What would you like to work on?"
    gen_b = "Hi! Ready to help with the ci-gate repo. What would you like to work on?"
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant(gen_a)
    t.stream_assistant(gen_b)
    t.finish_assistant(gen_b)
    assert _assistant_texts(t) == [gen_b]


def test_double_retry_restream_collapses_to_final_reply():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("Attempt one. ")
    t.stream_assistant("Attempt two. ")
    t.stream_assistant("Attempt three.")
    t.finish_assistant("Attempt three.")
    assert _assistant_texts(t) == ["Attempt three."]


def test_tail_fragment_final_does_not_truncate_streamed_answer():
    # Pathological provider: final text is only the last chunk of the stream.
    # The >=25% guard must keep the full streamed block intact.
    t = TuiTranscript()
    t.begin_turn()
    long_reply = "Step one does A. Step two does B. Step three does C. Finally, run the tests."
    t.stream_assistant(long_reply)
    t.finish_assistant("run the tests.")
    assert _assistant_texts(t) == [long_reply]


def test_matching_final_leaves_streamed_block_untouched():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("The answer is X.")
    t.finish_assistant("The answer is X.")
    assert _assistant_texts(t) == ["The answer is X."]


def test_finish_after_trace_does_not_duplicate_streamed_answer():
    # stream answer -> tool trace closes the block -> message_done re-emits it.
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("The answer is X.")
    t.append("trace", "✓ some_tool")
    t.finish_assistant("The answer is X.")
    assert _assistant_texts(t) == ["The answer is X."]  # exactly one copy


def test_finish_ignores_incidental_whitespace_when_deduping():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("Line one\nLine two")
    t.append("trace", "✓ tool")
    t.finish_assistant("Line one   Line two")  # same words, different spacing
    assert len(_assistant_texts(t)) == 1


def test_finish_still_appends_a_genuinely_different_answer():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("First answer.")
    t.append("trace", "✓ tool")
    t.finish_assistant("Second, different answer.")
    assert _assistant_texts(t) == ["First answer.", "Second, different answer."]


def test_identical_answers_in_separate_turns_are_kept():
    # A user boundary resets the turn — the same reply to a repeated question
    # must NOT be suppressed across turns.
    t = TuiTranscript()
    t.append_user("what is 2+2?")
    t.finish_assistant("4")
    t.append_user("what is 2+2?")
    t.finish_assistant("4")
    assert _assistant_texts(t) == ["4", "4"]


# ---------------------- render-layer collapse (safety net) ----------------------


def test_render_collapses_verbatim_repeat_within_turn():
    entries = [
        ("user", "do the thing"),
        ("assistant", "Done — here is the result."),
        ("trace", "✓ tool"),
        ("assistant", "Done — here is the result."),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == {3}


def test_render_never_collapses_the_live_streaming_block():
    # index 3 is still streaming (== streaming_index) → always shown, even if it
    # currently matches an earlier finished block.
    entries = [
        ("assistant", "partial"),
        ("trace", "✓ tool"),
        ("assistant", "partial"),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=2)
    assert skip == set()


def test_render_keeps_repeat_across_user_boundary():
    entries = [
        ("user", "q"),
        ("assistant", "same reply"),
        ("user", "q"),
        ("assistant", "same reply"),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == set()


def test_render_collapses_three_way_repeat():
    entries = [
        ("assistant", "repeat me"),
        ("trace", "t1"),
        ("assistant", "repeat me"),
        ("trace", "t2"),
        ("assistant", "repeat me"),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == {2, 4}


def test_render_ignores_blank_assistant_entries():
    entries = [("assistant", "   "), ("assistant", "real")]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == set()
