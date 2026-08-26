from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from alysis_code.ide import mcp_oauth_coordinator as coordinator_mod
from alysis_code.ide.mcp_oauth_coordinator import (
    EncryptedMcpOAuthTokenVault,
    McpOAuthCoordinatorConfig,
    McpOAuthCoordinatorError,
    McpOAuthIdeCoordinator,
    McpOAuthLoginRequest,
)
from alysis_code.ide.mcp_oauth_lifecycle import (
    AuthorizationCodeFlowRequest,
    McpOAuthFlowRegistry,
    OAuthFlowState,
    OAuthTokenSet,
    OAuthValidationError,
)
from alysis_code.mcp import token_store as token_store_mod
from alysis_code.mcp.oauth import McpAuthorizationServerMetadata
from alysis_code.mcp.oauth_store import (
    McpOAuthTokenRecord,
    load_oauth_token_record,
    mcp_oauth_token_store_path,
    save_oauth_token_record,
)


def _metadata() -> McpAuthorizationServerMetadata:
    return McpAuthorizationServerMetadata(
        issuer="https://identity.example.test",
        authorization_endpoint="https://identity.example.test/oauth/authorize",
        token_endpoint="https://identity.example.test/oauth/token",
        code_challenge_methods_supported=("S256",),
        response_types_supported=("code",),
        scopes_supported=("files.read", "files.write"),
    )


def _record(
    *,
    access_token: str = "raw-access-token-canary",
    refresh_token: str | None = "raw-refresh-token-canary",
) -> McpOAuthTokenRecord:
    now = datetime.now(UTC).replace(microsecond=0)
    return McpOAuthTokenRecord(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        expires_at=now + timedelta(hours=1),
        obtained_at=now,
        granted_scopes=("files.read",),
    )


def _request(server_id: str = "docs-mcp") -> McpOAuthLoginRequest:
    return McpOAuthLoginRequest(
        server_id=server_id,
        resource_server_url="https://mcp.example.test/rpc?ignored=query",
        authorization_server_url="https://identity.example.test",
        client_id="public-vscode-client",
        scopes=("files.read",),
    )


@pytest.fixture(autouse=True)
def _encrypted_test_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "config"))
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        token_store_mod,
        "_get_keyring_password",
        lambda service, account: values.get((service, account)),
    )
    monkeypatch.setattr(
        token_store_mod,
        "_set_keyring_password",
        lambda service, account, password: values.__setitem__((service, account), password),
    )


def _coordinator(
    tmp_path: Path,
    *,
    exchanger=None,
    config: McpOAuthCoordinatorConfig | None = None,
    revoker=None,
) -> McpOAuthIdeCoordinator:
    def discover(**kwargs):
        assert kwargs["resource_server_url"] == "https://mcp.example.test/rpc"
        assert kwargs["authorization_server_url"] == "https://identity.example.test"
        return _metadata()

    return McpOAuthIdeCoordinator(
        registry_path=tmp_path / "user-data" / "oauth-flows.sqlite3",
        vault=EncryptedMcpOAuthTokenVault(),
        config=config,
        discovery=discover,
        exchanger=exchanger or (lambda **_kwargs: _record()),
        revoker=revoker,
    )


def _authorization_parts(status) -> tuple[str, str, str, dict[str, list[str]]]:
    assert status.authorization_url is not None
    query = parse_qs(urlsplit(status.authorization_url).query)
    return query["redirect_uri"][0], query["state"][0], query["nonce"][0], query


def _send_callback(redirect_uri: str, **params: str) -> str:
    target = f"{redirect_uri}?{urlencode(params)}"
    with urllib.request.urlopen(target, timeout=3) as response:  # noqa: S310 - loopback fixture
        return response.read().decode("utf-8")


def _wait_status(
    coordinator: McpOAuthIdeCoordinator,
    flow_id: str,
    expected: OAuthFlowState,
    *,
    timeout: float = 5.0,
):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = coordinator.status(flow_id)
        if last.state is expected:
            return last
        time.sleep(0.02)
    raise AssertionError(f"flow did not reach {expected.value}; last={last!r}")


def _wait_listener_closed(redirect_uri: str, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(redirect_uri, timeout=0.2)  # noqa: S310 - loopback fixture
        except (OSError, urllib.error.URLError):
            return
        time.sleep(0.02)
    raise AssertionError("OAuth callback listener remained open")


def test_authorization_flow_returns_immediately_and_completes_without_secret_surface(
    tmp_path: Path,
) -> None:
    exchanged: dict[str, object] = {}

    def exchange(**kwargs):
        exchanged.update(kwargs)
        return _record()

    coordinator = _coordinator(tmp_path, exchanger=exchange)
    try:
        started_at = time.monotonic()
        pending = coordinator.start_authorization_code(_request())
        elapsed = time.monotonic() - started_at
        redirect_uri, state, nonce, query = _authorization_parts(pending)

        assert elapsed < 2.0
        assert pending.state is OAuthFlowState.PENDING
        assert pending.expires_at - pending.created_at == pytest.approx(645.0)
        assert query["code_challenge_method"] == ["S256"]
        assert query["resource"] == ["https://mcp.example.test/rpc"]
        assert nonce
        public_before = json.dumps(pending.to_public_dict())
        assert "raw-access-token-canary" not in public_before
        assert "raw-refresh-token-canary" not in public_before
        assert "authorization_code_canary" not in public_before

        page = _send_callback(
            redirect_uri,
            code="authorization_code_canary",
            state=state,
        )
        assert "authorization_code_canary" not in page
        completed = _wait_status(coordinator, pending.flow_id, OAuthFlowState.COMPLETED)

        assert completed.authorization_url is None
        assert exchanged["code"] == "authorization_code_canary"
        assert exchanged["redirect_uri"] == redirect_uri
        assert exchanged["resource_server_url"] == "https://mcp.example.test/rpc"
        assert exchanged["code_verifier"]
        stored = load_oauth_token_record("docs-mcp")
        assert stored is not None
        assert stored.binding_id == pending.flow_id
        assert stored.access_token == "raw-access-token-canary"
        _wait_listener_closed(redirect_uri)

        public_after = json.dumps(completed.to_public_dict())
        assert "authorization_code_canary" not in public_after
        assert "raw-access-token-canary" not in public_after
        registry_files = list((tmp_path / "user-data").glob("oauth-flows.sqlite3*"))
        for registry_file in registry_files:
            raw = registry_file.read_bytes()
            assert b"authorization_code_canary" not in raw
            assert b"raw-access-token-canary" not in raw
            assert b"raw-refresh-token-canary" not in raw
    finally:
        coordinator.close()


def test_cancel_closes_listener_and_reaches_terminal_state(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    try:
        pending = coordinator.start_authorization_code(_request())
        redirect_uri, _, _, _ = _authorization_parts(pending)

        cancelled = coordinator.cancel(pending.flow_id)

        assert cancelled.state is OAuthFlowState.CANCELLED
        _wait_listener_closed(redirect_uri)
        assert load_oauth_token_record("docs-mcp") is None
    finally:
        coordinator.close()


def test_wrong_host_header_cannot_consume_callback(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    try:
        pending = coordinator.start_authorization_code(_request())
        redirect_uri, state, _, _ = _authorization_parts(pending)
        parsed = urlsplit(redirect_uri)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.putrequest(
            "GET",
            f"{parsed.path}?{urlencode({'code': 'attacker-code', 'state': state})}",
            skip_host=True,
        )
        connection.putheader("Host", "attacker.example.test")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()

        assert response.status == HTTPStatus.BAD_REQUEST
        assert coordinator.status(pending.flow_id).state is OAuthFlowState.PENDING
        _send_callback(redirect_uri, code="real-code", state=state)
        assert _wait_status(coordinator, pending.flow_id, OAuthFlowState.COMPLETED)
    finally:
        coordinator.close()


def test_provider_error_and_wrong_state_never_persist_callback_values(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    try:
        first = coordinator.start_authorization_code(_request("first-mcp"))
        redirect_uri, state, _, _ = _authorization_parts(first)
        page = _send_callback(
            redirect_uri,
            error="raw-provider-error-canary",
            error_description="raw-access-token-canary",
            state=state,
        )
        assert "raw-provider-error-canary" not in page
        denied = _wait_status(coordinator, first.flow_id, OAuthFlowState.FAILED)
        assert denied.error_code == "provider_denied"

        second = coordinator.start_authorization_code(_request("second-mcp"))
        second_redirect, second_state, _, _ = _authorization_parts(second)
        with pytest.raises(urllib.error.HTTPError) as rejected_request:
            _send_callback(
                second_redirect,
                code="attacker_authorization_code_canary",
                state="attacker-state-canary",
            )
        assert rejected_request.value.code == HTTPStatus.CONFLICT
        assert coordinator.status(second.flow_id).state is OAuthFlowState.PENDING
        _send_callback(
            second_redirect,
            code="authorization_code_canary",
            state=second_state,
        )
        assert _wait_status(coordinator, second.flow_id, OAuthFlowState.COMPLETED)

        for registry_file in (tmp_path / "user-data").glob("oauth-flows.sqlite3*"):
            raw = registry_file.read_bytes()
            assert b"raw-provider-error-canary" not in raw
            assert b"raw-access-token-canary" not in raw
            assert b"authorization_code_canary" not in raw
            assert b"attacker_authorization_code_canary" not in raw
            assert b"attacker-state-canary" not in raw
    finally:
        coordinator.close()


def test_logout_fences_inflight_exchange_and_prevents_token_resurrection(
    tmp_path: Path,
) -> None:
    exchange_started = threading.Event()
    release_exchange = threading.Event()

    def exchange(**_kwargs):
        exchange_started.set()
        assert release_exchange.wait(5)
        return _record()

    coordinator = _coordinator(tmp_path, exchanger=exchange)
    try:
        pending = coordinator.start_authorization_code(_request())
        redirect_uri, state, _, _ = _authorization_parts(pending)
        _send_callback(redirect_uri, code="authorization_code_canary", state=state)
        assert exchange_started.wait(3)

        result = coordinator.logout("docs-mcp")
        release_exchange.set()
        cancelled = _wait_status(coordinator, pending.flow_id, OAuthFlowState.CANCELLED)

        assert result.local_credentials_removed is False
        assert result.active_flows_cancelled == 1
        assert cancelled.error_code == "logout"
        assert load_oauth_token_record("docs-mcp") is None
    finally:
        release_exchange.set()
        coordinator.close()


def test_logout_revokes_completed_credentials_without_exposing_them(tmp_path: Path) -> None:
    revoked: list[OAuthTokenSet] = []
    coordinator = _coordinator(tmp_path, revoker=revoked.append)
    try:
        pending = coordinator.start_authorization_code(_request())
        redirect_uri, state, _, _ = _authorization_parts(pending)
        _send_callback(redirect_uri, code="authorization_code_canary", state=state)
        _wait_status(coordinator, pending.flow_id, OAuthFlowState.COMPLETED)

        result = coordinator.logout("docs-mcp")

        assert result.local_credentials_removed is True
        assert result.remote_revocation_attempted is True
        assert result.remote_revocation_succeeded is True
        assert len(revoked) == 1
        assert "raw-access-token-canary" not in repr(revoked[0])
        assert load_oauth_token_record("docs-mcp") is None
    finally:
        coordinator.close()


def test_exchange_exception_is_reduced_to_safe_error_code_and_listener_closes(
    tmp_path: Path,
) -> None:
    def exchange(**_kwargs):
        raise RuntimeError("authorization_code_canary raw-access-token-canary")

    coordinator = _coordinator(tmp_path, exchanger=exchange)
    try:
        pending = coordinator.start_authorization_code(_request())
        redirect_uri, state, _, _ = _authorization_parts(pending)
        _send_callback(redirect_uri, code="authorization_code_canary", state=state)

        failed = _wait_status(coordinator, pending.flow_id, OAuthFlowState.FAILED)

        assert failed.error_code == "token_exchange_failed"
        assert "authorization_code_canary" not in repr(failed)
        assert "raw-access-token-canary" not in repr(failed)
        assert load_oauth_token_record("docs-mcp") is None
        _wait_listener_closed(redirect_uri)
    finally:
        coordinator.close()


def test_close_cancels_pending_flows_closes_listener_and_rejects_new_work(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    pending = coordinator.start_authorization_code(_request())
    redirect_uri, _, _, _ = _authorization_parts(pending)

    coordinator.close()

    assert coordinator.status(pending.flow_id).state is OAuthFlowState.CANCELLED
    _wait_listener_closed(redirect_uri)
    with pytest.raises(McpOAuthCoordinatorError, match="closed"):
        coordinator.start_authorization_code(_request("other-mcp"))


def test_callback_timeout_is_reported_without_waiting_in_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        coordinator_mod._LoopbackCallbackListener,
        "wait",
        lambda self, *, timeout_seconds, cancel_event: None,
    )
    coordinator = _coordinator(tmp_path)
    try:
        pending = coordinator.start_authorization_code(_request())
        failed = _wait_status(coordinator, pending.flow_id, OAuthFlowState.FAILED)
        assert failed.error_code == "callback_timeout"
    finally:
        coordinator.close()


def test_encrypted_vault_round_trip_binding_and_backward_compatible_record() -> None:
    vault = EncryptedMcpOAuthTokenVault()
    binding_id = "A" * 43
    tokens = OAuthTokenSet(
        access_token="raw-access-token-canary",
        refresh_token="raw-refresh-token-canary",
        token_type="Bearer",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).timestamp(),
        scopes=("files.read",),
    )

    vault.store("docs-mcp", tokens, binding_id=binding_id)

    assert vault.has_binding("docs-mcp", binding_id=binding_id) is True
    assert vault.has_binding("docs-mcp", binding_id="B" * 43) is False
    loaded = vault.load("docs-mcp")
    assert loaded is not None
    assert loaded.access_token == "raw-access-token-canary"
    raw_envelope = mcp_oauth_token_store_path().read_text(encoding="utf-8")
    assert "raw-access-token-canary" not in raw_envelope
    assert "raw-refresh-token-canary" not in raw_envelope
    assert binding_id not in raw_envelope
    assert vault.delete("docs-mcp") is True

    legacy = _record(access_token="legacy-access", refresh_token=None)
    save_oauth_token_record("legacy-mcp", legacy)
    assert load_oauth_token_record("legacy-mcp") == legacy
    assert load_oauth_token_record("legacy-mcp").binding_id is None  # type: ignore[union-attr]


def test_coordinator_recovers_vault_bound_completion_after_restart(tmp_path: Path) -> None:
    registry_path = tmp_path / "user-data" / "oauth-flows.sqlite3"
    vault = EncryptedMcpOAuthTokenVault()
    redirect_uri = "http://127.0.0.1:8765/oauth/callback/recovery"
    registry = McpOAuthFlowRegistry(
        vault,
        registry_path,
        redirect_allowlist=(redirect_uri,),
    )
    pending = registry.start_authorization_code(
        AuthorizationCodeFlowRequest(
            server_id="docs-mcp",
            authorization_endpoint=_metadata().authorization_endpoint,
            token_endpoint=_metadata().token_endpoint,
            client_id="public-vscode-client",
            redirect_uri=redirect_uri,
            scopes=("files.read",),
            require_nonce=False,
        )
    )
    query = parse_qs(urlsplit(pending.authorization_url or "").query)
    registry.begin_completion(pending.flow_id, state=query["state"][0])
    record = _record()
    vault.store(
        "docs-mcp",
        OAuthTokenSet(
            access_token=record.access_token,
            refresh_token=record.refresh_token,
            token_type=record.token_type,
            expires_at=record.expires_at.timestamp(),
            scopes=record.granted_scopes,
        ),
        binding_id=pending.flow_id,
    )

    restarted = _coordinator(tmp_path)
    try:
        recovered = restarted.status(pending.flow_id)
        assert recovered.state is OAuthFlowState.COMPLETED
        assert recovered.authorization_url is None
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "resource_url",
    [
        "http://mcp.example.test/rpc",
        "https://user:secret@mcp.example.test/rpc",
        "file:///tmp/socket",
        "https://mcp.example.test/rpc#fragment",
    ],
)
def test_resource_url_validation_fails_closed_before_listener(
    tmp_path: Path, resource_url: str
) -> None:
    coordinator = _coordinator(tmp_path)
    try:
        request = McpOAuthLoginRequest(
            server_id="docs-mcp",
            resource_server_url=resource_url,
            client_id="public-vscode-client",
        )
        with pytest.raises(OAuthValidationError):
            coordinator.start_authorization_code(request)
    finally:
        coordinator.close()


def test_invalid_binding_id_never_reaches_encrypted_store() -> None:
    vault = EncryptedMcpOAuthTokenVault()
    tokens = OAuthTokenSet(
        access_token="raw-access-token-canary",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).timestamp(),
    )
    with pytest.raises(Exception, match="binding_id"):
        vault.store("docs-mcp", tokens, binding_id="short")
    assert mcp_oauth_token_store_path().exists() is False
