from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.metadata import credential_scope_fingerprint
from alysis_code.profiles import ProfileSpec


def _response(protocol: str) -> dict:
    if protocol == "anthropic_messages":
        return {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "connected"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
    if protocol == "openai_responses":
        return {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "connected"}],
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
    return {
        "id": "chat_test",
        "choices": [
            {
                "message": {"role": "assistant", "content": "connected"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


@pytest.mark.parametrize("base_path", ["/zen/v1", "/zen/go/v1/"])
@pytest.mark.parametrize(
    ("protocol", "endpoint"),
    [
        ("openai_compat", "chat/completions"),
        ("openai_responses", "responses"),
        ("anthropic_messages", "messages"),
    ],
)
def test_opencode_wire_header_survives_followup_and_client_recreation(
    base_path: str, protocol: str, endpoint: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response(protocol))

    cfg = AppConfig(model="test-model", prompt_cache_mode="off")
    profile = ProfileSpec(
        name="arbitrary-name", protocol=protocol, base_url=f"https://opencode.ai{base_path}"
    )
    transport = httpx.MockTransport(handler)
    clients = [
        make_llm_client(
            cfg=cfg,
            api_key="test-key",
            model="test-model",
            profile=profile,
            transport=transport,
            session_id=session,
        )
        for session in ("parent-session", "parent-session", "child-session")
    ]
    for client in clients:
        for question in ("first", "followup"):
            result = client.chat(messages=[{"role": "user", "content": question}], stream=False)
            assert result.content == "connected"

    assert len(requests) == 6
    assert all(r.url.path == f"{base_path.rstrip('/')}/{endpoint}" for r in requests)
    values = [r.headers["x-opencode-session"] for r in requests]
    assert len(set(values[:4])) == 1
    assert values[4] == values[5] != values[0]
    assert all(r.headers["user-agent"].startswith("alysis-code/") for r in requests)
    assert profile.extra_headers == {}
    assert clients[0].route_identity == clients[1].route_identity
    assert clients[0].route_identity != clients[2].route_identity
    assert dict(clients[0].route_identity.routing_headers)["x-opencode-session"] == (
        credential_scope_fingerprint(values[0])
    )
    assert values[0] not in str(clients[0].route_identity.as_metadata())


def test_opencode_standalone_clients_get_separate_retained_session_headers() -> None:
    cfg = AppConfig(model="test-model")
    profile = ProfileSpec(name="go", base_url="https://opencode.ai/zen/go/v1")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response("openai_compat"))

    first, second = [
        make_llm_client(
            cfg=cfg,
            api_key="test-key",
            model="test-model",
            profile=profile,
            transport=httpx.MockTransport(handler),
        )
        for _ in range(2)
    ]
    for client in (first, first, second):
        client.chat(messages=[{"role": "user", "content": "first"}], stream=False)
    assert requests[0].headers["x-opencode-session"] == requests[1].headers["x-opencode-session"]
    assert requests[0].headers["x-opencode-session"] != requests[2].headers["x-opencode-session"]


def test_explicit_session_header_is_case_insensitive_override() -> None:
    profile = ProfileSpec(
        name="go",
        base_url="https://opencode.ai/zen/go/v1",
        extra_headers={"X-OpenCode-Session": "user-selected-session", "X-Route": "kept"},
    )
    client = make_llm_client(
        cfg=AppConfig(model="test-model"),
        api_key="test-key",
        model="test-model",
        profile=profile,
        session_id="parent-session",
    )
    assert client.extra_headers == {
        "x-opencode-session": "user-selected-session",
        "x-route": "kept",
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "https://opencode.ai.example.test/zen/go/v1",
        "https://gateway.example.test/zen/go/v1",
        "https://opencode.ai/unrelated/v1",
        "http://opencode.ai/zen/go/v1",
    ],
)
def test_profile_name_does_not_send_opencode_session_to_unrelated_endpoint(base_url: str) -> None:
    client = make_llm_client(
        cfg=AppConfig(model="test-model"),
        api_key="test-key",
        model="test-model",
        profile=ProfileSpec(name="opencode", base_url=base_url),
        session_id="parent-session",
    )
    assert "x-opencode-session" not in client.extra_headers


@pytest.mark.parametrize("provider_session_id", ["retained-parent-session", None])
def test_session_title_uses_conversation_identity(monkeypatch, provider_session_id: str | None):
    from alysis_code.cli_impl.commands.chat_resume_helpers import (
        _generate_session_summary_with_model,
    )

    captured: dict = {}

    def client_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(chat=lambda **_: SimpleNamespace(content="Review parser fix"))

    monkeypatch.setattr("alysis_code.llm.factory.make_llm_client", client_factory)
    session = SimpleNamespace(
        cfg=AppConfig(model="test-model"),
        client=SimpleNamespace(api_key="test-key", model="test-model"),
        store=SimpleNamespace(session_id="parent-session"),
        provider_session_id=provider_session_id,
    )
    title = _generate_session_summary_with_model(
        session=session,
        transcript_messages=[{"role": "user", "content": "Review the parser fix"}],
    )
    assert title == "Review parser fix"
    assert captured["session_id"] == (provider_session_id or "parent-session")
