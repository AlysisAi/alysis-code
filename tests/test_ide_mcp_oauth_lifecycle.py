from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from alysis_code.ide.mcp_oauth_lifecycle import (
    AuthorizationCodeFlowRequest,
    DeviceCodeFlowRequest,
    McpOAuthFlowRegistry,
    OAuthCompletionRejectedError,
    OAuthFlowConflictError,
    OAuthFlowState,
    OAuthFlowStateError,
    OAuthLifecycleError,
    OAuthTokenSet,
    OAuthValidationError,
    OAuthVaultError,
)


class MutableClock:
    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class MemoryVault:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: dict[str, tuple[str, OAuthTokenSet]] = {}
        self.store_count = 0
        self.raise_on_store: Exception | None = None
        self.raise_on_load: Exception | None = None

    def store(self, server_id: str, tokens: OAuthTokenSet, *, binding_id: str) -> None:
        with self._lock:
            self.store_count += 1
            if self.raise_on_store is not None:
                raise self.raise_on_store
            self.records[server_id] = (binding_id, tokens)

    def load(self, server_id: str) -> OAuthTokenSet | None:
        if self.raise_on_load is not None:
            raise self.raise_on_load
        record = self.records.get(server_id)
        return record[1] if record else None

    def has_binding(self, server_id: str, *, binding_id: str) -> bool:
        record = self.records.get(server_id)
        return bool(record and record[0] == binding_id)

    def delete(self, server_id: str) -> bool:
        with self._lock:
            return self.records.pop(server_id, None) is not None


@pytest.fixture
def redirect_uri() -> str:
    return "http://127.0.0.1:8765/oauth/callback"


@pytest.fixture
def registry_parts(tmp_path: Path, redirect_uri: str):
    clock = MutableClock()
    vault = MemoryVault()
    path = tmp_path / "user-data" / "oauth.sqlite3"
    registry = McpOAuthFlowRegistry(
        vault,
        path,
        redirect_allowlist=(redirect_uri,),
        clock=clock,
    )
    return registry, vault, clock, path


def _auth_request(
    allowlisted_redirect_uri: str, **overrides: object
) -> AuthorizationCodeFlowRequest:
    values: dict[str, object] = {
        "server_id": "docs-mcp",
        "authorization_endpoint": "https://identity.example.test/oauth/authorize",
        "token_endpoint": "https://identity.example.test/oauth/token",
        "client_id": "public-vscode-client",
        "redirect_uri": allowlisted_redirect_uri,
        "scopes": ("files.read", "files.write"),
    }
    values.update(overrides)
    return AuthorizationCodeFlowRequest(**values)  # type: ignore[arg-type]


def _device_request(**overrides: object) -> DeviceCodeFlowRequest:
    values: dict[str, object] = {
        "server_id": "docs-mcp",
        "token_endpoint": "https://identity.example.test/oauth/token",
        "client_id": "public-vscode-client",
        "device_code": "secret-device-code-value",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://identity.example.test/device",
        "verification_uri_complete": "https://identity.example.test/device?user_code=ABCD-EFGH",
        "scopes": ("files.read",),
        "expires_in": 600.0,
        "polling_interval": 5.0,
    }
    values.update(overrides)
    return DeviceCodeFlowRequest(**values)  # type: ignore[arg-type]


def _authorization_proof(status) -> tuple[str, str, dict[str, list[str]]]:
    assert status.authorization_url is not None
    query = parse_qs(urlsplit(status.authorization_url).query)
    return query["state"][0], query["nonce"][0], query


def _tokens() -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token="raw-access-token-canary",
        refresh_token="raw-refresh-token-canary",
        expires_at=2_000_000.0,
        scopes=("files.read",),
    )


def test_authorization_start_uses_pkce_s256_state_nonce_and_no_browser(
    registry_parts, redirect_uri: str
) -> None:
    registry, _, _, _ = registry_parts

    status = registry.start_authorization_code(
        _auth_request(
            redirect_uri,
            extra_authorization_params={"prompt": "consent", "audience": "mcp"},
        )
    )

    assert status.state is OAuthFlowState.PENDING
    assert "docs-mcp" not in status.flow_id
    assert len(status.flow_id) >= 32
    state, nonce, query = _authorization_proof(status)
    assert len(state) >= 32
    assert len(nonce) >= 32
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["scope"] == ["files.read files.write"]
    assert query["prompt"] == ["consent"]
    assert registry.list(server_id="docs-mcp") == (status,)

    claim = registry.begin_completion(status.flow_id, state=state)
    verifier = claim.material.pkce_verifier
    assert verifier is not None
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert query["code_challenge"] == [expected.decode()]
    assert claim.material.expected_nonce == nonce


def test_authorization_complete_is_single_use_and_scrubs_flow_secrets(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, _, path = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, nonce, _ = _authorization_proof(pending)
    claim = registry.begin_completion(pending.flow_id, state=state)

    completed = registry.finish_completion(claim, _tokens(), nonce=nonce)

    assert completed.state is OAuthFlowState.COMPLETED
    assert completed.authorization_url is None
    assert vault.has_binding("docs-mcp", binding_id=pending.flow_id)
    with pytest.raises(OAuthFlowStateError, match="no longer active"):
        registry.finish_completion(claim, _tokens(), nonce=nonce)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT state_hash, nonce_hash, nonce_value, pkce_verifier, authorization_url "
            "FROM oauth_flows WHERE flow_id = ?",
            (pending.flow_id,),
        ).fetchone()
    assert row == (None, None, None, None, None)


def test_callback_state_and_token_nonce_are_both_validated(
    registry_parts, redirect_uri: str
) -> None:
    registry, _, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, nonce, _ = _authorization_proof(pending)

    with pytest.raises(OAuthCompletionRejectedError, match="rejected"):
        registry.begin_completion(pending.flow_id, state="attacker-state")
    assert registry.status(pending.flow_id).state is OAuthFlowState.PENDING

    claim = registry.begin_completion(pending.flow_id, state=state)
    with pytest.raises(OAuthCompletionRejectedError, match="nonce"):
        registry.finish_completion(claim, _tokens(), nonce="attacker-nonce")
    assert registry.status(pending.flow_id).state is OAuthFlowState.COMPLETING
    completed = registry.finish_completion(claim, _tokens(), nonce=nonce)
    assert completed.state is OAuthFlowState.COMPLETED


def test_convenience_completion_rejects_missing_nonce_without_consuming_flow(
    registry_parts, redirect_uri: str
) -> None:
    registry, _, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, _, _ = _authorization_proof(pending)

    with pytest.raises(OAuthCompletionRejectedError, match="incomplete"):
        registry.complete(pending.flow_id, _tokens(), state=state)

    assert registry.status(pending.flow_id).state is OAuthFlowState.PENDING


def test_nonce_can_be_explicitly_optional_for_non_oidc_oauth(
    registry_parts, redirect_uri: str
) -> None:
    registry, _, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri, require_nonce=False))
    state, _, _ = _authorization_proof(pending)

    claim = registry.begin_completion(pending.flow_id, state=state)
    assert registry.finish_completion(claim, _tokens()).state is OAuthFlowState.COMPLETED


def test_device_flow_exposes_only_user_metadata_and_internal_exchange_material(
    registry_parts,
) -> None:
    registry, _, _, path = registry_parts
    pending = registry.start_device_code(_device_request())

    assert pending.state is OAuthFlowState.PENDING
    public = pending.to_public_dict()
    assert public["user_code"] == "ABCD-EFGH"
    assert public["verification_uri"] == "https://identity.example.test/device"
    assert "device_code" not in public
    assert "secret-device-code-value" not in repr(pending)

    polling_material = registry.device_poll_material(pending.flow_id)
    assert polling_material.device_code == "secret-device-code-value"
    claim = registry.begin_completion(pending.flow_id)
    assert claim.material.device_code == polling_material.device_code
    assert "secret-device-code-value" not in repr(claim)
    assert registry.finish_completion(claim, _tokens()).state is OAuthFlowState.COMPLETED
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT device_code, user_code FROM oauth_flows WHERE flow_id = ?",
            (pending.flow_id,),
        ).fetchone()
    assert stored == (None, None)


def test_device_flow_remains_cancellable_while_polling(registry_parts) -> None:
    registry, _, _, _ = registry_parts
    pending = registry.start_device_code(_device_request())

    registry.device_poll_material(pending.flow_id)

    assert registry.cancel(pending.flow_id).state is OAuthFlowState.CANCELLED
    with pytest.raises(OAuthFlowStateError, match="not pending"):
        registry.device_poll_material(pending.flow_id)


def test_pending_flow_survives_restart(registry_parts, redirect_uri: str) -> None:
    registry, vault, clock, path = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))

    restarted = McpOAuthFlowRegistry(
        vault,
        path,
        redirect_allowlist=(redirect_uri,),
        clock=clock,
    )

    recovered = restarted.status(pending.flow_id)
    assert recovered.state is OAuthFlowState.PENDING
    assert recovered.authorization_url == pending.authorization_url


def test_restart_finalizes_interrupted_completion_when_vault_binding_exists(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, clock, path = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, _, _ = _authorization_proof(pending)
    registry.begin_completion(pending.flow_id, state=state)
    vault.store("docs-mcp", _tokens(), binding_id=pending.flow_id)

    restarted = McpOAuthFlowRegistry(
        vault,
        path,
        redirect_allowlist=(redirect_uri,),
        clock=clock,
    )

    assert restarted.status(pending.flow_id).state is OAuthFlowState.COMPLETED


def test_restart_fails_closed_after_interrupted_completion_lease(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, clock, path = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, _, _ = _authorization_proof(pending)
    registry.begin_completion(pending.flow_id, state=state)
    clock.advance(91)

    restarted = McpOAuthFlowRegistry(
        vault,
        path,
        redirect_allowlist=(redirect_uri,),
        clock=clock,
    )

    failed = restarted.status(pending.flow_id)
    assert failed.state is OAuthFlowState.FAILED
    assert failed.error_code == "completion_interrupted"

    vault.store("docs-mcp", _tokens(), binding_id=pending.flow_id)
    assert restarted.status(pending.flow_id).state is OAuthFlowState.COMPLETED


def test_expiry_is_durable_and_prevents_completion(registry_parts, redirect_uri: str) -> None:
    registry, _, clock, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri, expires_in=30.0))
    state, _, _ = _authorization_proof(pending)
    clock.advance(30)

    assert registry.status(pending.flow_id).state is OAuthFlowState.EXPIRED
    with pytest.raises(OAuthFlowStateError, match="not pending"):
        registry.begin_completion(pending.flow_id, state=state)


def test_cancel_is_idempotent_but_cannot_cancel_completed(
    registry_parts, redirect_uri: str
) -> None:
    registry, _, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    assert registry.cancel(pending.flow_id).state is OAuthFlowState.CANCELLED
    assert registry.cancel(pending.flow_id).state is OAuthFlowState.CANCELLED

    second = registry.start_authorization_code(_auth_request(redirect_uri))
    state, nonce, _ = _authorization_proof(second)
    registry.complete(second.flow_id, _tokens(), state=state, nonce=nonce)
    with pytest.raises(OAuthFlowStateError, match="can no longer"):
        registry.cancel(second.flow_id)


def test_pending_flow_can_fail_with_only_an_allowlisted_error_code(
    registry_parts, redirect_uri: str
) -> None:
    registry, _, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))

    failed = registry.fail_pending(pending.flow_id, error_code="provider_denied")

    assert failed.state is OAuthFlowState.FAILED
    assert failed.error_code == "provider_denied"
    assert failed.authorization_url is None
    with pytest.raises(OAuthValidationError):
        registry.fail_pending(pending.flow_id, error_code="provider said secret=leak")


def test_only_one_active_flow_per_server_even_across_registry_instances(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, clock, path = registry_parts
    other = McpOAuthFlowRegistry(
        vault,
        path,
        redirect_allowlist=(redirect_uri,),
        clock=clock,
    )
    barrier = threading.Barrier(2)

    def start(target: McpOAuthFlowRegistry) -> str:
        barrier.wait()
        return target.start_authorization_code(_auth_request(redirect_uri)).flow_id

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, registry), pool.submit(start, other)]
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

    assert sum(isinstance(value, str) for value in outcomes) == 1
    assert sum(isinstance(value, OAuthFlowConflictError) for value in outcomes) == 1


def test_concurrent_duplicate_completion_stores_tokens_once(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, nonce, _ = _authorization_proof(pending)
    claim = registry.begin_completion(pending.flow_id, state=state)
    barrier = threading.Barrier(2)

    def finish() -> OAuthFlowState:
        barrier.wait()
        return registry.finish_completion(claim, _tokens(), nonce=nonce).state

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finish), pool.submit(finish)]
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

    assert outcomes.count(OAuthFlowState.COMPLETED) == 1
    assert sum(isinstance(value, OAuthFlowStateError) for value in outcomes) == 1
    assert vault.store_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"authorization_endpoint": "http://identity.example.test/authorize"},
        {"authorization_endpoint": "https://user:pass@identity.example.test/authorize"},
        {"token_endpoint": "https://identity.example.test/token#fragment"},
        {"token_endpoint": "https://identity.example.test/token?access_token=leak"},
        {"redirect_uri": "http://localhost:8765/oauth/callback"},
        {"redirect_uri": "http://127.0.0.1:9999/oauth/callback"},
    ],
)
def test_authorization_urls_and_redirects_fail_closed(
    registry_parts, redirect_uri: str, overrides: dict[str, object]
) -> None:
    registry, _, _, _ = registry_parts
    with pytest.raises(OAuthValidationError):
        registry.start_authorization_code(_auth_request(redirect_uri, **overrides))


def test_reserved_authorization_parameters_cannot_be_overridden(
    registry_parts, redirect_uri: str
) -> None:
    registry, _, _, _ = registry_parts
    with pytest.raises(OAuthValidationError, match="reserved"):
        registry.start_authorization_code(
            _auth_request(redirect_uri, extra_authorization_params={"state": "attacker"})
        )
    with pytest.raises(OAuthValidationError, match="credentials"):
        registry.start_authorization_code(
            _auth_request(
                redirect_uri,
                extra_authorization_params={"refresh_token": "must-not-enter-url"},
            )
        )


def test_device_complete_uri_must_share_verification_origin(registry_parts) -> None:
    registry, _, _, _ = registry_parts
    with pytest.raises(OAuthValidationError, match="origin"):
        registry.start_device_code(
            _device_request(
                verification_uri_complete="https://phishing.example.test/device?user_code=ABCD"
            )
        )


def test_registry_rejects_workspace_storage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(OAuthValidationError, match="outside"):
        McpOAuthFlowRegistry(
            MemoryVault(),
            workspace / ".alysis" / "oauth.sqlite3",
            workspace_roots=(workspace,),
        )


def test_secret_values_never_enter_repr_public_payload_database_or_errors(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, _, path = registry_parts
    tokens = _tokens()
    assert "raw-access-token-canary" not in repr(tokens)
    assert "raw-refresh-token-canary" not in repr(tokens)

    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, nonce, _ = _authorization_proof(pending)
    claim = registry.begin_completion(pending.flow_id, state=state)
    assert claim.material.pkce_verifier not in repr(claim)
    assert claim.material.expected_nonce not in repr(claim)
    assert "access_token" not in json.dumps(pending.to_public_dict())

    vault.raise_on_store = RuntimeError("provider leaked raw-access-token-canary")
    with pytest.raises(OAuthVaultError) as caught:
        registry.finish_completion(claim, tokens, nonce=nonce)
    assert "raw-access-token-canary" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw-access-token-canary" not in path.read_bytes().decode("latin1")
    assert "raw-refresh-token-canary" not in path.read_bytes().decode("latin1")


def test_logout_revokes_when_possible_but_always_removes_local_credentials(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, nonce, _ = _authorization_proof(pending)
    registry.complete(pending.flow_id, _tokens(), state=state, nonce=nonce)
    active = registry.start_authorization_code(_auth_request(redirect_uri))

    seen: list[OAuthTokenSet] = []

    def failing_revoker(tokens: OAuthTokenSet) -> None:
        seen.append(tokens)
        raise RuntimeError("provider response included raw-access-token-canary")

    result = registry.logout("docs-mcp", revoker=failing_revoker)

    assert len(seen) == 1
    assert result.local_credentials_removed is True
    assert result.active_flows_cancelled == 1
    assert result.remote_revocation_attempted is True
    assert result.remote_revocation_succeeded is False
    assert result.error_code == "remote_revocation_failed"
    assert vault.load("docs-mcp") is None
    assert registry.status(active.flow_id).state is OAuthFlowState.CANCELLED


def test_cleanup_removes_old_terminal_rows(registry_parts, redirect_uri: str) -> None:
    registry, _, clock, _ = registry_parts
    first = registry.start_authorization_code(_auth_request(redirect_uri))
    registry.cancel(first.flow_id)
    clock.advance(60)

    assert registry.cleanup(terminal_retention_seconds=30) == 1
    with pytest.raises(OAuthLifecycleError, match="not found"):
        registry.status(first.flow_id)


def test_vault_failure_is_sanitized_and_flow_fails_closed(
    registry_parts, redirect_uri: str
) -> None:
    registry, vault, _, _ = registry_parts
    pending = registry.start_authorization_code(_auth_request(redirect_uri))
    state, nonce, _ = _authorization_proof(pending)
    claim = registry.begin_completion(pending.flow_id, state=state)
    vault.raise_on_store = RuntimeError("raw-refresh-token-canary")

    with pytest.raises(OAuthVaultError, match="stored securely"):
        registry.finish_completion(claim, _tokens(), nonce=nonce)

    failed = registry.status(pending.flow_id)
    assert failed.state is OAuthFlowState.FAILED
    assert failed.error_code == "vault_store_failed"
