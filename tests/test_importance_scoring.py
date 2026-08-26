from __future__ import annotations

from alysis_code.compaction.importance import extract_text, score_text, score_turn


def test_score_turn_prioritizes_high_signal_requirement_turn() -> None:
    high_signal_turn = [
        {
            "role": "user",
            "content": (
                "MUST keep public API stable. Acceptance criteria: pytest -q passes and "
                "README.md is updated. Do not change CLI flags."
            ),
        },
        {"role": "assistant", "content": "I will follow the constraints and verify."},
    ]
    low_signal_turn = [
        {"role": "user", "content": "ok thanks"},
        {"role": "assistant", "content": "great"},
    ]

    high_score, high_reasons, _ = score_turn(high_signal_turn)
    low_score, low_reasons, _ = score_turn(low_signal_turn)

    assert high_score > low_score
    assert "requirements_or_constraints" in high_reasons
    assert "acceptance_criteria" in high_reasons
    assert low_reasons == []


def test_extract_text_handles_multipart_with_images() -> None:
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Check this screenshot"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            {"type": "text", "text": "and confirm"},
        ],
    }
    text = extract_text(msg)
    assert "Check this screenshot" in text
    assert "<image>" in text
    assert "and confirm" in text


def test_score_text_detects_error_and_diff_signals() -> None:
    text = (
        "Traceback (most recent call last): ... ERROR. diff --git a/x.py b/x.py\n"
        "@@ -1,1 +1,2 @@\npytest -q failed"
    )
    score, reasons = score_text(text)
    assert score > 0
    assert "errors_or_failures" in reasons
    assert "diff_or_patch" in reasons
    assert "verification_commands" in reasons
