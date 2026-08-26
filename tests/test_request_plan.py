from __future__ import annotations

import json

from alysis_code.llm.request_plan import LLMRequestPlan, RequestCachePlan


def test_request_plan_metadata_is_count_hash_and_estimate_only() -> None:
    plan = LLMRequestPlan.from_chat_args(
        messages=[
            {"role": "system", "content": "hidden system prompt"},
            {
                "role": "assistant",
                "content": "prior",
                "_alysis_provider_metadata": {"openai_responses": {"response_id": "rsp_1"}},
            },
            {"role": "user", "content": "secret user text"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup_secret",
                    "description": "secret tool description",
                    "parameters": {"type": "object"},
                },
            }
        ],
        cache=RequestCachePlan(strategy="openai_prompt_cache", mode="automatic"),
    )

    metadata = plan.request_plan_metadata(
        input_mode="full",
        provider_payload={"messages": [{"role": "user", "content": "secret user text"}]},
        sent_provider_payload={"messages": [{"role": "user", "content": "secret user text"}]},
        cache_policy_metadata={"strategy": "openai_prompt_cache", "mode": "automatic"},
    )

    assert metadata["schema_version"] == 1
    assert metadata["request_message_count"] == 3
    assert metadata["tool_count"] == 1
    assert metadata["stable_prefix_message_count"] == 2
    assert metadata["dynamic_suffix_message_count"] == 1
    assert metadata["provider_metadata_message_count"] == 1
    assert metadata["cache_strategy"] == "openai_prompt_cache"
    assert metadata["cache_mode"] == "automatic"
    assert metadata["cacheable_prefix_hash"]
    assert metadata["request_messages_signature"]
    assert metadata["tool_schema_hash"]
    assert metadata["serialized_request_estimate_tokens"] > 0
    assert metadata["sent_serialized_request_estimate_tokens"] > 0

    rendered = json.dumps(metadata, sort_keys=True)
    assert "hidden system prompt" not in rendered
    assert "secret user text" not in rendered
    assert "secret tool description" not in rendered
