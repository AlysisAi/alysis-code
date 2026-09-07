from __future__ import annotations

import pytest

from alysis_code.llm import streaming
from alysis_code.llm.streaming import iter_sse_frames, parse_sse_json_frame
from alysis_code.llm.types import LLMError


def test_iter_sse_frames_handles_multiline_data_and_done_sentinel() -> None:
    frames = list(
        iter_sse_frames(
            [
                "event: message_delta",
                'data: {"a":',
                "data: 1}",
                "",
                "data: [DONE]",
                "",
                'data: {"ignored": true}',
                "",
            ]
        )
    )

    assert len(frames) == 1
    assert frames[0].event == "message_delta"
    assert frames[0].data == '{"a":\n1}'


def test_parse_sse_json_frame_reports_malformed_payload() -> None:
    frame = next(iter_sse_frames(["event: content_block_delta", "data: {bad", ""]))

    with pytest.raises(LLMError, match="malformed JSON.*content_block_delta"):
        parse_sse_json_frame(frame, stream_name="test stream")


def test_text_chunk_line_framing_does_not_recopy_the_buffer_per_fragment() -> None:
    class _CopyCountingText(str):
        copied_characters = 0

        def __add__(self, other: object) -> _CopyCountingText:
            type(self).copied_characters += len(self)
            return type(self)(super().__add__(str(other)))

        def __radd__(self, other: object) -> _CopyCountingText:
            prefix = str(other)
            type(self).copied_characters += len(prefix)
            return type(self)(prefix + str(self))

    fragment_count = 4_096
    chunks = iter([*(_CopyCountingText("x") for _ in range(fragment_count)), "\n"])

    lines = list(streaming.iter_lines_from_text_chunks(chunks))

    assert lines == ["x" * fragment_count + "\n"]
    assert _CopyCountingText.copied_characters < fragment_count * 4
