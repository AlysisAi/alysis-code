from __future__ import annotations

import pytest

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
