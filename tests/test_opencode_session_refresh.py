from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.metadata import credential_scope_fingerprint
from alysis_code.profiles import ProfileSpec, add_profile, get_active_profile, set_active_profile


@pytest.fixture(autouse=True)
def _isolated_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "alysis_code.config.resolve_api_key",
        lambda _cfg: SimpleNamespace(key="fixture-key", source="test"),
    )

    def reject_real_http(*_args, **_kwargs):
        pytest.fail("Refresh regression tests must use MockTransport.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_real_http)
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(chat_loop, name, lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(
        "alysis_code.agent.prompt_context.refresh_session_prompt_guidance",
        lambda _session: False,
    )


def _cfg(protocol: str, base_url: str, extra_headers: dict[str, str] | None = None) -> AppConfig:
    cfg = AppConfig(model="fixture-model", prompt_cache_mode="off")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="arbitrary-gateway-name",
            protocol=protocol,
            base_url=base_url,
            extra_headers=extra_headers or {},
        ),
    )
    set_active_profile(cfg, "arbitrary-gateway-name")
    return cfg


def _session(cfg: AppConfig, protocol: str, requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if protocol == "anthropic_messages":
            data = {
                "id": "fixture-message",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ready"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }
        elif protocol == "openai_responses":
            data = {
                "id": "fixture-response",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ready"}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }
        else:
            data = {
                "id": "fixture-completion",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ready"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        return httpx.Response(200, json=data)

    clients = [
        make_llm_client(
            cfg=cfg,
            api_key="fixture-key",
            model=cfg.model,
            transport=httpx.MockTransport(handler),
            session_id="retained-provider-session",
        )
        for _ in range(2)
    ]
    return SimpleNamespace(
        cfg=cfg,
        client=clients[0],
        conversation_compactor=SimpleNamespace(compactor_client=clients[1]),
        provider_session_id="retained-provider-session",
        store=SimpleNamespace(session_id="new-local-log-after-resume"),
        mode="readonly",
    )


def _send_main_and_compactor(session) -> None:
    for client in (session.client, session.conversation_compactor.compactor_client):
        assert (
            client.chat(messages=[{"role": "user", "content": "Check."}], stream=False).content
            == "ready"
        )


@pytest.mark.parametrize("base_path", ["/zen/v1", "/zen/go/v1/"])
@pytest.mark.parametrize("protocol", ["openai_compat", "openai_responses", "anthropic_messages"])
def test_unchanged_refresh_keeps_wire_session_and_exact_main_compactor_routes(
    protocol: str, base_path: str
) -> None:
    cfg = _cfg(protocol, f"https://opencode.ai{base_path}", {"X-Route": "unchanged"})
    configured_headers = dict(get_active_profile(cfg).extra_headers)
    requests: list[httpx.Request] = []
    session = _session(cfg, protocol, requests)
    clients = (session.client, session.conversation_compactor.compactor_client)
    before = [client.route_identity for client in clients]
    _send_main_and_compactor(session)

    for _ in range(2):
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)
        _send_main_and_compactor(session)

    expected = f"alysis-{credential_scope_fingerprint('retained-provider-session')}"
    assert len(requests) == 6
    assert [request.headers.get("x-opencode-session") for request in requests] == [expected] * 6
    assert all(request.headers["x-route"] == "unchanged" for request in requests)
    assert [client.route_identity for client in clients] == before
    assert all(
        dict(client.route_identity.routing_headers)["x-opencode-session"]
        == credential_scope_fingerprint(expected)
        for client in clients
    )
    assert get_active_profile(session.cfg).extra_headers == configured_headers
    assert "x-opencode-session" not in configured_headers


def test_refresh_removes_generated_header_off_route_and_honors_explicit_override() -> None:
    protocol = "openai_compat"
    requests: list[httpx.Request] = []
    session = _session(_cfg(protocol, "https://opencode.ai/zen/go/v1"), protocol, requests)
    original_route = session.client.route_identity
    _send_main_and_compactor(session)

    elsewhere = _cfg(protocol, "https://gateway.example.test/v1", {"X-Route": "elsewhere"})
    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=elsewhere)
    _send_main_and_compactor(session)
    assert all("x-opencode-session" not in request.headers for request in requests[2:4])
    assert session.client.route_identity != original_route

    explicit = _cfg(
        protocol,
        "https://opencode.ai/zen/go/v1",
        {"X-OpenCode-Session": "explicit-session"},
    )
    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=explicit)
    _send_main_and_compactor(session)
    assert [request.headers["x-opencode-session"] for request in requests[4:6]] == [
        "explicit-session",
        "explicit-session",
    ]
    explicit_route = session.client.route_identity
    assert explicit_route != original_route

    chat_loop._apply_config_menu_changes_to_session(
        session=session, cfg=_cfg(protocol, "https://opencode.ai/zen/go/v1")
    )
    _send_main_and_compactor(session)
    assert [request.headers.get("x-opencode-session") for request in requests[6:8]] == [
        requests[0].headers["x-opencode-session"],
        requests[0].headers["x-opencode-session"],
    ]
    assert session.client.route_identity == original_route
    assert session.conversation_compactor.compactor_client.route_identity == original_route
