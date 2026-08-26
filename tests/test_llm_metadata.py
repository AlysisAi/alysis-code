from __future__ import annotations

import json

import pytest

from alysis_code.llm import metadata, openai_compat
from alysis_code.llm.types import LLMResponse, ToolCall


def test_openai_compat_reexports_shared_provider_metadata_helpers() -> None:
    assert openai_compat.PROVIDER_METADATA_KEY == metadata.PROVIDER_METADATA_KEY
    assert (
        openai_compat.attach_provider_metadata_to_assistant_message
        is metadata.attach_provider_metadata_to_assistant_message
    )
    assert (
        openai_compat.strip_provider_metadata_from_message
        is metadata.strip_provider_metadata_from_message
    )
    assert openai_compat.assistant_message_from_response is metadata.assistant_message_from_response


def test_shared_metadata_helpers_attach_and_strip_provider_state() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call_1",
                name="fs_read",
                arguments={"path": "README.md"},
                provider_metadata={"gemini_generate_content": {"thoughtSignature": "sig_1"}},
            )
        ],
        raw={},
        provider_metadata={"openai_responses": {"response_id": "resp_1"}},
    )

    message = metadata.assistant_message_from_response(response)

    assert message[metadata.PROVIDER_METADATA_KEY] == {
        "openai_responses": {"response_id": "resp_1"},
        "_tool_calls": [
            {
                "id": "call_1",
                "index": 0,
                "metadata": {"gemini_generate_content": {"thoughtSignature": "sig_1"}},
            }
        ],
    }
    assert metadata.strip_provider_metadata_from_message(message) == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "fs_read", "arguments": '{"path": "README.md"}'},
            }
        ],
    }


def test_deepseek_state_is_retained_for_normal_assistant_messages() -> None:
    response = LLMResponse(
        content="Public answer.",
        tool_calls=[],
        raw={},
        provider_metadata={"deepseek": {"reasoning_content": "opaque continuation state"}},
    )

    message = metadata.assistant_message_from_response(response)

    assert message[metadata.PROVIDER_METADATA_KEY]["deepseek"] == {
        "reasoning_content": "opaque continuation state"
    }
    assert "reasoning_content" not in message
    assert metadata.strip_provider_metadata_from_message(message) == {
        "role": "assistant",
        "content": "Public answer.",
    }


def test_shared_metadata_helpers_deep_copy_nested_provider_state() -> None:
    output_items = [
        {
            "type": "web_search_call",
            "id": "ws_1",
            "action": {"type": "search", "query": "Alysis Code metadata"},
        }
    ]
    tool_extra_content = {
        "google": {
            "thought_signature": "original-signature",
            "grounding": [{"uri": "https://example.test/original"}],
        }
    }
    response = LLMResponse(
        content="Hosted search answer.",
        tool_calls=[
            ToolCall(
                id="call_1",
                name="fs_read",
                arguments={"path": "README.md"},
                provider_metadata={
                    "gemini": {
                        "extra_content": tool_extra_content,
                    }
                },
            )
        ],
        raw={},
        provider_metadata={
            "openai_responses": {
                "response_id": "resp_1",
                "output_items": output_items,
            }
        },
    )

    message = metadata.assistant_message_from_response(response)

    output_items[0]["id"] = "mutated"
    tool_extra_content["google"]["thought_signature"] = "mutated-signature"
    tool_extra_content["google"]["grounding"][0]["uri"] = "https://example.test/mutated"

    stored_metadata = message[metadata.PROVIDER_METADATA_KEY]
    assert stored_metadata["openai_responses"]["output_items"][0]["id"] == "ws_1"
    stored_tool_metadata = stored_metadata["_tool_calls"][0]["metadata"]
    assert (
        stored_tool_metadata["gemini"]["extra_content"]["google"]["thought_signature"]
        == "original-signature"
    )
    assert (
        stored_tool_metadata["gemini"]["extra_content"]["google"]["grounding"][0]["uri"]
        == "https://example.test/original"
    )


def _route(**overrides: object) -> metadata.ProviderRouteIdentity:
    values: dict[str, object] = {
        "protocol": "openai_responses",
        "base_url": "https://api.example.test/v1/",
        "provider_key": "example",
        "model": "reasoning-model",
        "profile_name": "work",
        "auth_provider": "subscription-adapter",
        "credential_scope": metadata.credential_scope_fingerprint("secret-api-key"),
        "routing_headers": {
            "OpenAI-Organization": "org-secret",
            "X-Diagnostic": "ignored",
        },
        "routing_fields": {"session_id": "sticky-secret"},
        "reasoning_state_adapter": "openrouter_reasoning",
        "protocol_revision": "2026-05-20",
        "session_scope": metadata.credential_scope_fingerprint("session-secret"),
    }
    values.update(overrides)
    return metadata.build_provider_route_identity(**values)  # type: ignore[arg-type]


def test_route_identity_normalizes_base_url_and_persists_only_digest() -> None:
    first = _route(base_url="HTTPS://API.EXAMPLE.TEST/v1/#fragment")
    second = _route(base_url="https://api.example.test/v1")

    assert first.fingerprint == second.fingerprint
    serialized = json.dumps(first.as_metadata(), sort_keys=True)
    assert set(first.as_metadata()) == {"version", "fingerprint"}
    assert "secret-api-key" not in serialized
    assert "org-secret" not in serialized
    assert "session-secret" not in serialized
    assert "sticky-secret" not in serialized
    assert "api.example.test" not in serialized

    query_a = _route(base_url="https://api.example.test/v1?region=us&api-version=2")
    query_b = _route(base_url="https://API.EXAMPLE.TEST/v1/?api-version=2&region=us")
    assert query_a.fingerprint == query_b.fingerprint
    assert (
        _route(
            routing_headers={
                "OpenAI-Organization": "org-secret",
                "X-Diagnostic": "one",
            }
        ).fingerprint
        != _route(
            routing_headers={
                "OpenAI-Organization": "org-secret",
                "X-Diagnostic": "two",
            }
        ).fingerprint
    )


def test_route_headers_share_transport_normalization_and_reject_case_duplicates() -> None:
    spaced = _route(
        routing_headers={" X-Tenant-ID ": "  tenant-a  "},
    )
    canonical = _route(
        routing_headers={"x-tenant-id": "tenant-a"},
    )

    assert spaced.fingerprint == canonical.fingerprint
    assert spaced.routing_headers == canonical.routing_headers
    with pytest.raises(ValueError, match="Duplicate extra header name"):
        _route(
            routing_headers={
                "X-Tenant-ID": "tenant-a",
                "x-tenant-id": "tenant-b",
            }
        )


def test_endpoint_descriptor_persists_only_digest_and_sanitized_host() -> None:
    secret_url = (
        "https://User:Password@API.Example.Test/private/signed-token"
        "?api_key=query-secret&region=us#fragment-secret"
    )

    descriptor = metadata.endpoint_descriptor(secret_url)
    serialized = json.dumps(descriptor, sort_keys=True)

    assert set(descriptor) == {"version", "fingerprint", "host"}
    assert descriptor["version"] == 1
    assert descriptor["host"] == "api.example.test"
    assert len(descriptor["fingerprint"]) == 64
    assert metadata.endpoint_descriptor_matches(secret_url, descriptor) is True
    assert (
        metadata.endpoint_descriptor_matches(
            "https://User:Password@API.Example.Test/private/other-token"
            "?api_key=query-secret&region=us",
            descriptor,
        )
        is False
    )
    for secret in (
        "User",
        "Password",
        "private",
        "signed-token",
        "api_key",
        "query-secret",
        "fragment-secret",
    ):
        assert secret not in serialized


def test_endpoint_and_route_digests_preserve_case_sensitive_userinfo() -> None:
    upper = "https://User:CaseSensitive@api.example.test/v1"
    lower = "https://User:casesensitive@api.example.test/v1"

    assert metadata.endpoint_descriptor(upper) != metadata.endpoint_descriptor(lower)
    assert _route(base_url=upper).fingerprint != _route(base_url=lower).fingerprint


@pytest.mark.parametrize(
    "descriptor",
    [
        None,
        {},
        {"version": 2, "fingerprint": "0" * 64},
        {"version": 1, "fingerprint": "not-a-digest"},
    ],
)
def test_endpoint_descriptor_match_fails_closed_for_invalid_metadata(
    descriptor: object,
) -> None:
    assert metadata.endpoint_descriptor_matches("https://api.example.test/v1", descriptor) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol", "anthropic_messages"),
        ("base_url", "https://other.example.test/v1"),
        ("provider_key", "other"),
        ("model", "other-model"),
        ("profile_name", "personal"),
        ("auth_provider", "other-adapter"),
        ("credential_scope", metadata.credential_scope_fingerprint("other-key")),
        ("routing_headers", {"OpenAI-Project": "other-project"}),
        ("routing_fields", {"x-session-id": "other-sticky-session"}),
        ("reasoning_state_adapter", "deepseek_reasoning"),
        ("protocol_revision", "2027-01-01"),
        ("session_scope", metadata.credential_scope_fingerprint("other-session")),
    ],
)
def test_route_identity_changes_for_every_route_dimension(field: str, value: object) -> None:
    assert _route(**{field: value}).fingerprint != _route().fingerprint


def test_route_gate_keeps_only_exact_stamped_state_and_deep_copies() -> None:
    route = _route()
    provider_state = {
        "openai_responses": {
            "response_id": "resp_1",
            "output_items": [{"type": "reasoning", "encrypted_content": "opaque"}],
        }
    }
    stamped = metadata.stamp_provider_metadata_for_route(provider_state, route)
    messages = [
        {
            "role": "assistant",
            "content": "public answer",
            metadata.PROVIDER_METADATA_KEY: stamped,
        }
    ]

    matching = metadata.gate_messages_for_provider_route(messages, route)
    mismatched = metadata.gate_messages_for_provider_route(
        messages,
        _route(model="other-model"),
    )
    stamped["openai_responses"]["response_id"] = "mutated"

    assert matching[0][metadata.PROVIDER_METADATA_KEY]["openai_responses"]["response_id"] == (
        "resp_1"
    )
    assert metadata.PROVIDER_METADATA_KEY not in mismatched[0]
    assert mismatched[0]["content"] == "public answer"


def test_sanitize_urls_for_output_removes_all_route_material() -> None:
    sentinel = "PRIVATE_URL_SENTINEL"
    raw = (
        "failed at https://route-user:route-password@api.example.test:8443/"
        f"private/{sentinel}?token={sentinel}#{sentinel}."
    )

    sanitized = metadata.sanitize_urls_for_output(raw)

    assert sanitized.startswith("failed at api.example.test:8443 (endpoint ")
    assert sanitized.endswith(".")
    for secret in (
        "route-user",
        "route-password",
        "private",
        sentinel,
        "token=",
    ):
        assert secret not in sanitized


def test_route_gate_strips_legacy_unstamped_and_top_level_reasoning_state() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "public answer",
            "reasoning_content": "raw chain",
            "reasoning": "raw reasoning",
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}],
            metadata.PROVIDER_METADATA_KEY: {"deepseek": {"reasoning_content": "raw chain"}},
        }
    ]

    assert metadata.gate_messages_for_provider_route(messages, _route()) == [
        {"role": "assistant", "content": "public answer"}
    ]


def test_response_route_stamp_covers_tool_call_only_metadata_without_bloating_plain_text() -> None:
    route = _route()
    tool_response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call_1",
                name="fs_read",
                arguments={},
                provider_metadata={
                    "gemini_generate_content": {"thoughtSignature": "opaque-signature"}
                },
            )
        ],
        raw={},
    )
    plain_response = LLMResponse(content="hello", tool_calls=[], raw={})

    stamped = metadata.stamp_response_for_route(tool_response, route)

    assert stamped.provider_metadata == {
        metadata.ROUTE_IDENTITY_PROVIDER_METADATA_KEY: route.as_metadata()
    }
    assistant = metadata.assistant_message_from_response(stamped)
    assert (
        assistant[metadata.PROVIDER_METADATA_KEY][metadata.ROUTE_IDENTITY_PROVIDER_METADATA_KEY]
        == route.as_metadata()
    )
    assert metadata.stamp_response_for_route(plain_response, route).provider_metadata is None
