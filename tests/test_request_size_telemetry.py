from __future__ import annotations

from alysis_code.request_estimation import (
    RequestTokenBreakdown,
    estimate_provider_payload_tokens,
    estimate_request_token_breakdown,
)


def test_request_token_breakdown_splits_bootstrap_history_tools_memory_and_pins() -> None:
    messages = [
        {"role": "system", "content": "Core coding rules."},
        {"role": "system", "content": "Repository context."},
        {"role": "user", "content": "Investigate failing parser tests."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "search_rg",
                    "arguments": {"pattern": "ParserError"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"matches":[{"path":"tests/test_parser.py","line":22,"text":"raise ParserError()"}]}',
        },
        {
            "role": "user",
            "content": '<<<ALYSIS_CONVERSATION_MEMORY_JSON>>>\n{"summary":"parser bug affects nested forms"}',
        },
        {
            "role": "user",
            "content": '<<<ALYSIS_CONVERSATION_PINS_JSON>>>\n[{"path":"src/parser.py"}]',
        },
        {"role": "assistant", "content": "The bug is in the nested expression branch."},
    ]
    tool_list = [
        {
            "type": "function",
            "function": {
                "name": "search_rg",
                "description": "Search workspace",
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                },
            },
        }
    ]

    breakdown = estimate_request_token_breakdown(
        messages=messages,
        tool_list=tool_list,
        pinned_prefix_len=2,
    )

    assert breakdown.bootstrap_prompt_tokens > 0
    assert breakdown.tool_schema_tokens > 0
    assert breakdown.live_conversation_history_tokens > 0
    assert breakdown.inline_tool_transcript_tokens > 0
    assert breakdown.memory_summary_tokens > 0
    assert breakdown.pins_tokens > 0
    assert breakdown.tool_schema_budget is not None
    assert breakdown.tool_schema_budget.tool_count == 1
    assert breakdown.tool_schema_budget.total_tokens == breakdown.tool_schema_tokens
    assert breakdown.tool_schema_budget.signature
    assert breakdown.tool_schema_budget.largest_tools[0].name == "search_rg"
    assert breakdown.total_tokens == (
        breakdown.bootstrap_prompt_tokens
        + breakdown.tool_schema_tokens
        + breakdown.live_conversation_history_tokens
        + breakdown.inline_tool_transcript_tokens
        + breakdown.memory_summary_tokens
        + breakdown.pins_tokens
    )

    round_tripped = RequestTokenBreakdown.from_payload(breakdown.to_payload())
    assert round_tripped is not None
    assert round_tripped.tool_schema_budget is not None
    assert round_tripped.tool_schema_budget.signature == breakdown.tool_schema_budget.signature
    assert round_tripped.tool_schema_budget.largest_tools[0].name == "search_rg"


def test_provider_payload_estimate_omits_data_url_payload_bodies() -> None:
    tiny = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                ],
            }
        ]
    }
    huge = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe"},
                    {"type": "input_image", "image_url": "data:image/png;base64," + ("A" * 8000)},
                ],
            }
        ]
    }

    assert estimate_provider_payload_tokens(tiny) == estimate_provider_payload_tokens(huge)
