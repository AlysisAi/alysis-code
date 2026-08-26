from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.protocols import (
    OPENAI_RESPONSES_PROTOCOL,
    resolve_reasoning_trace_capability,
)
from alysis_code.mcp import token_store as token_store_mod
from alysis_code.provider_auth.base import ProviderAuthError, ProviderLoginRequiredError
from alysis_code.provider_auth.openai_codex import (
    SESSION_EXPIRED_DETAIL,
    OpenAICodexSubscriptionAuth,
)
from alysis_code.provider_auth.store import (
    ProviderTokenRecord,
    load_provider_token,
    provider_token_store_path,
    save_provider_token,
)


def test_subscription_store_uses_random_filesystem_key_without_tui_stderr_noise(
    tmp_path, monkeypatch, capsys, caplog
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))

    def _keyring_unavailable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _keyring_unavailable)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _keyring_unavailable)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-123",
    )

    with caplog.at_level("WARNING", logger=token_store_mod.__name__):
        save_provider_token("openai-codex", record)
        loaded = [load_provider_token("openai-codex") for _ in range(3)]

    path = provider_token_store_path()
    envelope = json.loads(path.read_text(encoding="utf-8"))
    key_path = token_store_mod.filesystem_master_key_path(path)
    captured = capsys.readouterr()
    assert loaded == [record, record, record]
    assert envelope["version"] == token_store_mod.CURRENT_ENVELOPE_VERSION
    assert envelope["key_source"] == token_store_mod.KEY_SOURCE_FILESYSTEM
    assert len(key_path.read_bytes()) == 32
    assert "access-secret" not in path.read_text(encoding="utf-8")
    assert "refresh-secret" not in path.read_text(encoding="utf-8")
    assert "weak-derived-fallback" not in caplog.text
    assert captured.err == ""


def test_codex_subscription_payload_is_stateless_and_preserves_encrypted_reasoning() -> None:
    adapter = OpenAICodexSubscriptionAuth()

    payload = adapter.adapt_responses_payload(
        {
            "model": "gpt-test",
            "input": [
                {"role": "system", "content": "system prompt"},
                {"role": "developer", "content": [{"type": "input_text", "text": "dev"}]},
                {"role": "user", "content": "hello"},
                {
                    "id": "reasoning-server-id",
                    "status": "completed",
                    "type": "reasoning",
                    "encrypted_content": "opaque-state",
                    "summary": [],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer", "minimum": 1, "default": 2},
                            "opaque": False,
                            "values": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                        },
                        "required": ["count"],
                    },
                    "strict": True,
                }
            ],
            "reasoning": {"effort": "high"},
            "include": ["web_search_call.action.sources"],
            "temperature": 0.2,
            "max_output_tokens": 123,
            "previous_response_id": "resp_old",
            "stream": True,
        }
    )

    assert payload["instructions"] == "system prompt\n\ndev"
    assert payload["input"][0] == {"role": "user", "content": "hello"}
    assert payload["input"][1] == {
        "type": "reasoning",
        "encrypted_content": "opaque-state",
        "summary": [],
    }
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["tools"][0]["strict"] is False
    assert payload["tools"][0]["parameters"] == {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "opaque": {"type": "string"},
            "values": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["count"],
    }
    assert payload["include"] == [
        "web_search_call.action.sources",
        "reasoning.encrypted_content",
    ]
    assert payload["text"] == {"verbosity": "low"}
    assert "temperature" not in payload
    assert "max_output_tokens" not in payload
    assert "previous_response_id" not in payload


def test_codex_subscription_does_not_request_summary_without_trace() -> None:
    adapter = OpenAICodexSubscriptionAuth()

    payload = adapter.adapt_responses_payload(
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "hello"}],
            "stream": True,
        }
    )

    assert "reasoning" not in payload


def test_codex_subscription_preserves_explicit_summary_request_and_effort() -> None:
    adapter = OpenAICodexSubscriptionAuth()

    payload = adapter.adapt_responses_payload(
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "hello"}],
            "reasoning": {"effort": "high", "summary": "auto"},
            "stream": True,
        }
    )

    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}


def test_codex_subscription_headers_are_destination_allowlisted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ALYSIS_OPENAI_CODEX_COMPAT_VERSION", raising=False)
    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-123",
        account_label="developer@example.test",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: record,
    )
    adapter = OpenAICodexSubscriptionAuth()

    headers = adapter.authorization_headers(
        "https://chatgpt.com/backend-api/codex/responses",
        session_id="session-1",
    )

    assert headers["Authorization"] == "Bearer access-secret"
    assert headers["ChatGPT-Account-Id"] == "account-123"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["User-Agent"] == "codex_cli_rs/0.144.6"
    assert headers["session-id"] == "session-1"
    with pytest.raises(ProviderAuthError, match="non-Codex destination"):
        adapter.authorization_headers("https://example.com/responses")
    with pytest.raises(ProviderAuthError, match="non-Codex destination"):
        adapter.authorization_headers(
            "https://chatgpt.com/backend-api/codex/responses?redirect=https://example.com"
        )


def test_codex_subscription_refresh_rotates_tokens(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=time.time() - 1,
        account_id="account-1",
    )
    saved: list[ProviderTokenRecord] = []
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: saved[-1] if saved else stored,
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.save_provider_token",
        lambda _provider_id, record: saved.append(record),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://auth.openai.com/oauth/token"
        assert b"refresh_token=old-refresh" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    adapter = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler))
    headers = adapter.authorization_headers("https://chatgpt.com/backend-api/codex/responses")

    assert headers["Authorization"] == "Bearer new-access"
    assert saved[-1].refresh_token == "new-refresh"
    assert saved[-1].account_id == "account-1"


@pytest.mark.parametrize("status_code", [429, 500])
def test_codex_refresh_transient_failure_uses_still_valid_token(
    monkeypatch,
    status_code: int,
) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="still-valid",
        refresh_token="refresh",
        expires_at=time.time() + 120,
        account_id="account-1",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json={"error": "temporarily_unavailable"})
        )
    )

    headers = adapter.authorization_headers("https://chatgpt.com/backend-api/codex/responses")

    assert headers["Authorization"] == "Bearer still-valid"


def test_codex_refresh_transient_failure_does_not_reuse_expired_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="expired",
        refresh_token="refresh",
        expires_at=time.time() - 1,
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, json={}))
    )

    with pytest.raises(ProviderAuthError, match="temporarily unavailable"):
        adapter.authorization_headers("https://chatgpt.com/backend-api/codex/responses")


def test_codex_refresh_invalid_grant_requires_login(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stored = ProviderTokenRecord(
        access_token="expired",
        refresh_token="refresh",
        expires_at=time.time() - 1,
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )

    with pytest.raises(ProviderLoginRequiredError, match="session expired"):
        adapter.authorization_headers("https://chatgpt.com/backend-api/codex/responses")


def test_codex_account_status_reports_an_expired_session_as_data(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A status probe answers; only the model transport raises."""

    stored = ProviderTokenRecord(
        access_token="expired",
        refresh_token="refresh",
        expires_at=time.time() - 1,
        account_label="dev@example.test",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: stored,
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )

    status = adapter.account_status()

    assert status.connected is False
    assert status.verified is True
    assert status.detail == SESSION_EXPIRED_DETAIL
    assert status.account_label == "dev@example.test"


def test_codex_account_status_survives_an_unreadable_credential_store(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A spawned process that cannot reach the keyring must still get an answer."""

    def _unreadable(_provider_id: str) -> ProviderTokenRecord:
        raise ProviderAuthError("Could not read the encrypted provider credential store.")

    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        _unreadable,
    )

    status = OpenAICodexSubscriptionAuth().account_status()

    assert status.connected is False
    assert status.verified is False
    assert status.detail == "Could not read the encrypted provider credential store."


def test_codex_account_status_survives_an_unexpected_store_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _explode(_provider_id: str) -> ProviderTokenRecord:
        raise MemoryError("simulated")

    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        _explode,
    )

    status = OpenAICodexSubscriptionAuth().account_status()

    assert status.connected is False
    assert status.verified is False
    assert "MemoryError" in str(status.detail)


def test_codex_model_catalog_uses_codex_compat_version_and_live_metadata(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ALYSIS_OPENAI_CODEX_COMPAT_VERSION", raising=False)
    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=time.time() + 3600,
        account_id="account-123",
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: record,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["client_version"] == "0.144.6"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "gpt-codex-live",
                        "display_name": "GPT Codex Live",
                        "description": "Account model",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 1,
                        "context_window": 272000,
                        "input_modalities": ["text", "image"],
                        "default_reasoning_level": "high",
                        "supported_reasoning_levels": [
                            {"effort": "low", "description": "Fast"},
                            {"effort": "high", "description": "Deep"},
                        ],
                    },
                    {
                        "slug": "gpt-chatgpt-only",
                        "display_name": "GPT ChatGPT Only",
                        "visibility": "list",
                        "supported_in_api": False,
                        "priority": 2,
                        "supported_reasoning_levels": [
                            {"effort": "medium", "description": "Balanced"},
                        ],
                    },
                ]
            },
        )

    models = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler)).list_models(
        refresh=True
    )

    assert len(models) == 2
    assert models[0].id == "gpt-codex-live"
    assert models[0].is_default is True
    assert models[0].context_window_tokens == 272_000
    assert models[0].input_modalities == ("text", "image")
    assert [effort.id for effort in models[0].reasoning_efforts] == ["low", "high"]
    assert models[1].id == "gpt-chatgpt-only"
    assert [effort.id for effort in models[1].reasoning_efforts] == ["medium"]


class _FakeCodexAuth:
    provider_id = "fake-codex"
    display_name = "Fake"
    description = "Fake"
    base_url = "https://chatgpt.com/backend-api/codex"
    protocol = "openai_responses"
    supports_previous_response_id = False
    supports_temperature = False

    def __init__(self) -> None:
        self.force_refresh_values: list[bool] = []
        self.session_ids: list[str | None] = []
        self._dialect = OpenAICodexSubscriptionAuth()

    def authorization_headers(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
    ) -> Mapping[str, str]:
        self.force_refresh_values.append(force_refresh)
        self.session_ids.append(session_id)
        return {
            "Authorization": f"Bearer {'new' if force_refresh else 'old'}",
            "ChatGPT-Account-Id": "account-1",
            "originator": "alysis",
        }

    def adapt_responses_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._dialect.adapt_responses_payload(payload)


class _FakeStreamingCodexAuth(_FakeCodexAuth):
    requires_streaming = True


def _attach_subscription_reasoning_capability(
    client: OpenAIResponsesClient,
) -> OpenAIResponsesClient:
    client.reasoning_trace_capability = resolve_reasoning_trace_capability(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model_supports_reasoning=True,
    )
    return client


def test_subscription_non_public_stream_requests_and_surfaces_reasoning_summary() -> None:
    auth = _FakeStreamingCodexAuth()
    captured: dict[str, Any] = {}
    summaries: list[str] = []
    reasoning_item = {
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "Checked the constraints."}],
    }

    def sse(event_type: str, data: dict[str, Any]) -> bytes:
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        events = [
            (
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "rs_subscription",
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": "Checked the constraints.",
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_subscription_summary",
                        "model": "gpt-test",
                        "status": "completed",
                        "output_text": "Done.",
                        "output": [reasoning_item],
                    },
                },
            ),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join(sse(event_type, data) for event_type, data in events),
        )

    client = _attach_subscription_reasoning_capability(
        OpenAIResponsesClient(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="",
            model="gpt-test",
            reasoning_effort="high",
            provider_auth=auth,
            transport=httpx.MockTransport(handler),
        )
    )

    response = client.chat(
        messages=[{"role": "user", "content": "inspect"}],
        stream=False,
        on_reasoning_delta=summaries.append,
    )

    assert captured["stream"] is True
    assert captured["reasoning"] == {"effort": "high", "summary": "auto"}
    assert response.content == "Done."
    assert [item.text for item in response.reasoning] == ["Checked the constraints."]
    assert summaries == ["Checked the constraints."]


def test_responses_client_retries_one_401_with_refreshed_subscription_auth() -> None:
    auth = _FakeCodexAuth()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="",
        model="gpt-test",
        prompt_cache_key="session-1",
        reasoning_effort="medium",
        provider_auth=auth,
        session_id="agent-session-1",
        transport=httpx.MockTransport(handler),
    )
    result = client.chat(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hi"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert result.content == "hello"
    assert auth.force_refresh_values == [False, True]
    assert auth.session_ids == ["agent-session-1", "agent-session-1"]
    assert requests[0].headers["authorization"] == "Bearer old"
    assert requests[1].headers["authorization"] == "Bearer new"
    sent = json.loads(requests[1].content)
    assert sent["instructions"] == "system"
    assert sent["store"] is False
    assert sent["reasoning"] == {"effort": "medium"}
    assert sent["tools"][0]["strict"] is False
    assert "previous_response_id" not in sent
    assert "temperature" not in sent
