from __future__ import annotations

from alysis_code.model_registry import ModelMeta
from alysis_code.token_budget import (
    _TOKEN_ENCODING_NAME,
    _fallback_estimate_tokens,
    compute_input_budget,
    estimate_tokens,
    trim_text_to_budget,
)


def test_trim_text_to_budget_keeps_head_and_tail_with_marker() -> None:
    text = "HEAD " + ("middle " * 4000) + " TAIL"
    trimmed, was_trimmed = trim_text_to_budget(text, max_tokens=120)
    assert was_trimmed is True
    assert "HEAD" in trimmed
    assert "TAIL" in trimmed
    assert "[TRUNCATED FOR TOKEN BUDGET]" in trimmed


def test_compute_input_budget_with_clamp() -> None:
    cap = ModelMeta(
        model_name="tiny",
        context_window_tokens=2048,
        max_output_tokens=1800,
    )
    budget = compute_input_budget(cap, safety_margin=512)
    assert budget == 512


def test_compute_input_budget_shared_window_keeps_input_room() -> None:
    # Kimi Code metadata declares max_output == context (a shared window, no
    # fixed output reservation). The budget must reserve a fraction, not the
    # whole window — regression for the fresh-session "context: 0% left" bug.
    cap = ModelMeta(
        model_name="k3",
        context_window_tokens=1_048_576,
        max_output_tokens=1_048_576,
    )
    budget = compute_input_budget(cap, safety_margin=512)
    assert budget == 1_048_576 - (1_048_576 // 8) - 512
    cap_small = ModelMeta(
        model_name="kimi-for-coding",
        context_window_tokens=262_144,
        max_output_tokens=262_144,
    )
    assert compute_input_budget(cap_small, safety_margin=512) == 262_144 - 32_768 - 512


def test_estimate_tokens_returns_positive_for_non_empty() -> None:
    assert estimate_tokens("hello world") > 0


def test_estimate_tokens_uses_tiktoken_encoder() -> None:
    import tiktoken

    text = "hello world\nκαλημέρα κόσμε"
    expected = len(tiktoken.get_encoding(_TOKEN_ENCODING_NAME).encode(text))

    assert estimate_tokens(text) == expected


def test_fallback_estimate_tokens_counts_token_like_pieces_not_characters() -> None:
    text = "hello, world!"

    assert _fallback_estimate_tokens(text) == 6
    assert _fallback_estimate_tokens(text) < len(text)
