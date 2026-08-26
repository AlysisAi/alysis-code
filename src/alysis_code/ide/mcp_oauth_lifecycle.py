"""Durable, protocol-safe OAuth lifecycle primitives for IDE-managed MCP login.

This module deliberately does not open a browser, listen on a callback socket,
perform HTTP requests, or serialize credentials.  It owns the security-sensitive
state machine around those operations while callers supply the UI and token
exchange adapters.

Only short-lived flow material is stored in the SQLite registry.  Access and
refresh tokens are handed directly to an injected :class:`OAuthTokenVault` and
are excluded from dataclass representations, public payloads, exceptions, and
the registry database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TypeAlias
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..branding import canonical_user_data_dir, env_get

__all__ = [
    "AuthorizationCodeFlowRequest",
    "DeviceCodeFlowRequest",
    "McpOAuthFlowRegistry",
    "OAuthCompletionClaim",
    "OAuthCompletionRejectedError",
    "OAuthExchangeMaterial",
    "OAuthFlowConflictError",
    "OAuthFlowKind",
    "OAuthFlowNotFoundError",
    "OAuthFlowState",
    "OAuthFlowStateError",
    "OAuthFlowStatus",
    "OAuthLifecycleError",
    "OAuthLogoutResult",
    "OAuthTokenSet",
    "OAuthTokenVault",
    "OAuthValidationError",
    "OAuthVaultError",
    "default_oauth_flow_registry_path",
]

JsonScalar: TypeAlias = None | bool | int | float | str

SCHEMA_VERSION = 1
DEFAULT_FLOW_TTL_SECONDS = 10 * 60.0
DEFAULT_COMPLETION_LEASE_SECONDS = 90.0
DEFAULT_TERMINAL_RETENTION_SECONDS = 24 * 60 * 60.0
MAX_FLOW_TTL_SECONDS = 30 * 60.0
MAX_URL_LENGTH = 4096
MAX_SCOPES = 64

_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SCOPE_RE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]{1,256}$")
_FLOW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_RESERVED_AUTH_PARAMS = frozenset(
    {
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "nonce",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
    }
)
_SENSITIVE_URL_PARAM_NAMES = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "code",
        "device_code",
        "id_token",
        "refresh_token",
        "token",
    }
)


class OAuthFlowKind(str, Enum):
    AUTHORIZATION_CODE = "authorization_code"
    DEVICE_CODE = "device_code"


class OAuthFlowState(str, Enum):
    PENDING = "pending"
    COMPLETING = "completing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {
        OAuthFlowState.COMPLETED,
        OAuthFlowState.CANCELLED,
        OAuthFlowState.FAILED,
        OAuthFlowState.EXPIRED,
    }
)


class OAuthLifecycleError(RuntimeError):
    """Base error whose message is safe for logs and protocol error objects."""


class OAuthValidationError(OAuthLifecycleError, ValueError):
    pass


class OAuthFlowConflictError(OAuthLifecycleError):
    pass


class OAuthFlowNotFoundError(OAuthLifecycleError):
    pass


class OAuthFlowStateError(OAuthLifecycleError):
    pass


class OAuthCompletionRejectedError(OAuthLifecycleError):
    pass


class OAuthVaultError(OAuthLifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    """Credentials that must only move between an exchange adapter and a vault."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = field(default="Bearer", repr=False)
    expires_at: float | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not self.access_token.strip():
            raise OAuthValidationError("OAuth access token is missing.")
        if self.refresh_token is not None and (
            not isinstance(self.refresh_token, str) or not self.refresh_token.strip()
        ):
            raise OAuthValidationError("OAuth refresh token is invalid.")
        if not isinstance(self.token_type, str) or not self.token_type.strip():
            raise OAuthValidationError("OAuth token type is invalid.")
        if self.expires_at is not None and (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(float(self.expires_at))
        ):
            raise OAuthValidationError("OAuth token expiry is invalid.")
        object.__setattr__(self, "scopes", _normalize_scopes(self.scopes))


class OAuthTokenVault(Protocol):
    """Secret-store boundary required by :class:`McpOAuthFlowRegistry`.

    ``binding_id`` is the flow id.  Implementations must save the credentials
    atomically and preserve the binding so a restarted registry can determine
    whether an interrupted completion reached the vault.
    """

    def store(self, server_id: str, tokens: OAuthTokenSet, *, binding_id: str) -> None: ...

    def load(self, server_id: str) -> OAuthTokenSet | None: ...

    def has_binding(self, server_id: str, *, binding_id: str) -> bool: ...

    def delete(self, server_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class AuthorizationCodeFlowRequest:
    server_id: str
    authorization_endpoint: str
    token_endpoint: str
    client_id: str = field(repr=False)
    redirect_uri: str
    scopes: tuple[str, ...] = field(default_factory=tuple)
    extra_authorization_params: Mapping[str, JsonScalar] = field(default_factory=dict, repr=False)
    expires_in: float = DEFAULT_FLOW_TTL_SECONDS
    require_nonce: bool = True


@dataclass(frozen=True, slots=True)
class DeviceCodeFlowRequest:
    server_id: str
    token_endpoint: str
    client_id: str = field(repr=False)
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_uri: str
    verification_uri_complete: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = field(default_factory=tuple)
    expires_in: float = DEFAULT_FLOW_TTL_SECONDS
    polling_interval: float = 5.0


@dataclass(frozen=True, slots=True)
class OAuthFlowStatus:
    flow_id: str
    server_id: str
    kind: OAuthFlowKind
    state: OAuthFlowState
    created_at: float
    updated_at: float
    expires_at: float
    authorization_url: str | None = field(default=None, repr=False)
    verification_uri: str | None = None
    verification_uri_complete: str | None = field(default=None, repr=False)
    user_code: str | None = field(default=None, repr=False)
    polling_interval: float | None = None
    terminal_at: float | None = None
    error_code: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Return the allowlisted payload suitable for an IDE protocol response."""

        payload: dict[str, Any] = {
            "flow_id": self.flow_id,
            "server_id": self.server_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "terminal_at": self.terminal_at,
            "error_code": self.error_code,
        }
        if self.kind is OAuthFlowKind.AUTHORIZATION_CODE:
            payload["authorization_url"] = self.authorization_url
        else:
            payload.update(
                {
                    "verification_uri": self.verification_uri,
                    "verification_uri_complete": self.verification_uri_complete,
                    "user_code": self.user_code,
                    "polling_interval": self.polling_interval,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class OAuthExchangeMaterial:
    flow_id: str
    server_id: str
    kind: OAuthFlowKind
    token_endpoint: str
    client_id: str = field(repr=False)
    redirect_uri: str | None = None
    pkce_verifier: str | None = field(default=None, repr=False)
    expected_nonce: str | None = field(default=None, repr=False)
    device_code: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class OAuthCompletionClaim:
    flow_id: str
    claim_token: str = field(repr=False)
    material: OAuthExchangeMaterial = field(repr=False)
    lease_expires_at: float


@dataclass(frozen=True, slots=True)
class OAuthLogoutResult:
    server_id: str
    local_credentials_removed: bool
    active_flows_cancelled: int
    remote_revocation_attempted: bool
    remote_revocation_succeeded: bool | None
    error_code: str | None = None


def default_oauth_flow_registry_path() -> Path:
    override = str(env_get("ALYSIS_DATA_DIR") or "").strip()
    data_dir = Path(override).expanduser() if override else canonical_user_data_dir()
    return data_dir / "ide" / "mcp-oauth-flows.sqlite3"


class McpOAuthFlowRegistry:
    """SQLite-backed OAuth flow registry with fenced single-use completion."""

    def __init__(
        self,
        vault: OAuthTokenVault,
        path: str | os.PathLike[str] | None = None,
        *,
        redirect_allowlist: Iterable[str] = (),
        workspace_roots: Iterable[str | os.PathLike[str]] = (),
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        completion_lease_seconds: float = DEFAULT_COMPLETION_LEASE_SECONDS,
    ) -> None:
        if vault is None:
            raise OAuthValidationError("A secure OAuth token vault is required.")
        self.vault = vault
        self.path = Path(path) if path is not None else default_oauth_flow_registry_path()
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(32))
        self._owner_id = secrets.token_urlsafe(24)
        self._completion_lease_seconds = _bounded_duration(
            completion_lease_seconds,
            field_name="completion lease",
            minimum=1.0,
            maximum=10 * 60.0,
        )
        self._redirect_allowlist = frozenset(
            _validate_redirect_uri(value) for value in redirect_allowlist
        )
        self._lock = threading.RLock()
        self._validate_storage_location(workspace_roots)
        self._prepare_storage()
        self.recover()

    def start_authorization_code(self, request: AuthorizationCodeFlowRequest) -> OAuthFlowStatus:
        self.recover()
        server_id = _validate_server_id(request.server_id)
        authorization_endpoint = _validate_https_url(
            request.authorization_endpoint, field_name="authorization endpoint"
        )
        token_endpoint = _validate_https_url(request.token_endpoint, field_name="token endpoint")
        redirect_uri = _validate_redirect_uri(request.redirect_uri)
        if not self._redirect_allowlist or redirect_uri not in self._redirect_allowlist:
            raise OAuthValidationError("OAuth redirect URI is not allowlisted.")
        client_id = _validate_client_id(request.client_id)
        scopes = _normalize_scopes(request.scopes)
        expires_in = _bounded_duration(
            request.expires_in,
            field_name="flow expiry",
            minimum=30.0,
            maximum=MAX_FLOW_TTL_SECONDS,
        )
        extras = _normalize_extra_params(request.extra_authorization_params)
        if not isinstance(request.require_nonce, bool):
            raise OAuthValidationError("OAuth nonce policy is invalid.")
        flow_id = self._new_flow_id()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        pkce_verifier = secrets.token_urlsafe(64)[:96]
        code_challenge = _pkce_challenge(pkce_verifier)
        authorization_url = _build_authorization_url(
            authorization_endpoint,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
            extras=extras,
        )
        now = self._now()
        expires_at = now + expires_in
        row = {
            "flow_id": flow_id,
            "server_id": server_id,
            "kind": OAuthFlowKind.AUTHORIZATION_CODE.value,
            "state": OAuthFlowState.PENDING.value,
            "authorization_url": authorization_url,
            "token_endpoint": token_endpoint,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scopes_json": _encode_scopes(scopes),
            "state_hash": _secret_digest(state),
            "nonce_hash": _secret_digest(nonce),
            "nonce_value": nonce,
            "nonce_required": int(bool(request.require_nonce)),
            "pkce_verifier": pkce_verifier,
            "device_code": None,
            "verification_uri": None,
            "verification_uri_complete": None,
            "user_code": None,
            "polling_interval": None,
            "created_at": now,
            "updated_at": now,
            "expires_at": expires_at,
        }
        self._insert_flow(row)
        return self.status(flow_id)

    def start_device_code(self, request: DeviceCodeFlowRequest) -> OAuthFlowStatus:
        self.recover()
        server_id = _validate_server_id(request.server_id)
        token_endpoint = _validate_https_url(request.token_endpoint, field_name="token endpoint")
        client_id = _validate_client_id(request.client_id)
        device_code = _required_secret(request.device_code, field_name="device code")
        user_code = _required_public_string(
            request.user_code, field_name="device user code", maximum=256
        )
        verification_uri = _validate_https_url(
            request.verification_uri, field_name="device verification URI"
        )
        verification_uri_complete = None
        if request.verification_uri_complete is not None:
            verification_uri_complete = _validate_https_url(
                request.verification_uri_complete,
                field_name="complete device verification URI",
            )
            if _url_origin(verification_uri_complete) != _url_origin(verification_uri):
                raise OAuthValidationError(
                    "Complete device verification URI must use the verification origin."
                )
        scopes = _normalize_scopes(request.scopes)
        expires_in = _bounded_duration(
            request.expires_in,
            field_name="flow expiry",
            minimum=30.0,
            maximum=MAX_FLOW_TTL_SECONDS,
        )
        polling_interval = _bounded_duration(
            request.polling_interval,
            field_name="device polling interval",
            minimum=1.0,
            maximum=60.0,
        )
        flow_id = self._new_flow_id()
        now = self._now()
        row = {
            "flow_id": flow_id,
            "server_id": server_id,
            "kind": OAuthFlowKind.DEVICE_CODE.value,
            "state": OAuthFlowState.PENDING.value,
            "authorization_url": None,
            "token_endpoint": token_endpoint,
            "client_id": client_id,
            "redirect_uri": None,
            "scopes_json": _encode_scopes(scopes),
            "state_hash": None,
            "nonce_hash": None,
            "nonce_value": None,
            "nonce_required": 0,
            "pkce_verifier": None,
            "device_code": device_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": verification_uri_complete,
            "user_code": user_code,
            "polling_interval": polling_interval,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + expires_in,
        }
        self._insert_flow(row)
        return self.status(flow_id)

    def status(self, flow_id: str) -> OAuthFlowStatus:
        canonical_flow_id = _validate_flow_id(flow_id)
        self.recover(flow_id=canonical_flow_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_flows WHERE flow_id = ?", (canonical_flow_id,)
            ).fetchone()
        if row is None:
            raise OAuthFlowNotFoundError("OAuth flow was not found.")
        return _status_from_row(row)

    def list(
        self, *, server_id: str | None = None, limit: int = 100
    ) -> tuple[OAuthFlowStatus, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 500:
            raise OAuthValidationError("OAuth flow list limit is outside the allowed range.")
        canonical_server_id = _validate_server_id(server_id) if server_id is not None else None
        self.recover()
        query = "SELECT * FROM oauth_flows"
        params: tuple[Any, ...]
        if canonical_server_id is None:
            params = (limit,)
        else:
            query += " WHERE server_id = ?"
            params = (canonical_server_id, limit)
        query += " ORDER BY created_at DESC, flow_id DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_status_from_row(row) for row in rows)

    def begin_completion(
        self,
        flow_id: str,
        *,
        state: str | None = None,
        nonce: str | None = None,
    ) -> OAuthCompletionClaim:
        canonical_flow_id = _validate_flow_id(flow_id)
        self.recover(flow_id=canonical_flow_id)
        now = self._now()
        claim_token = secrets.token_urlsafe(32)
        claim_hash = _secret_digest(claim_token)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_flows WHERE flow_id = ?", (canonical_flow_id,)
            ).fetchone()
            if row is None:
                raise OAuthFlowNotFoundError("OAuth flow was not found.")
            current = OAuthFlowState(str(row["state"]))
            if current is not OAuthFlowState.PENDING:
                raise OAuthFlowStateError("OAuth flow is not pending completion.")
            if now >= float(row["expires_at"]):
                self._set_terminal(
                    connection,
                    canonical_flow_id,
                    OAuthFlowState.EXPIRED,
                    now=now,
                    error_code="flow_expired",
                )
                # Preserve the expiry transition even though the transaction
                # context will see the state error raised below.
                connection.commit()
                raise OAuthFlowStateError("OAuth flow has expired.")
            kind = OAuthFlowKind(str(row["kind"]))
            if kind is OAuthFlowKind.AUTHORIZATION_CODE:
                self._validate_callback_proof(row, state=state, nonce=nonce)
            elif state is not None or nonce is not None:
                raise OAuthCompletionRejectedError(
                    "Device-code completion does not accept callback proof."
                )
            lease_expires_at = (
                float(row["expires_at"])
                if kind is OAuthFlowKind.DEVICE_CODE
                else min(float(row["expires_at"]), now + self._completion_lease_seconds)
            )
            connection.execute(
                """
                UPDATE oauth_flows
                   SET state = ?, updated_at = ?, completion_owner = ?,
                       completion_claim_hash = ?, completion_lease_expires_at = ?,
                       error_code = NULL
                 WHERE flow_id = ? AND state = ?
                """,
                (
                    OAuthFlowState.COMPLETING.value,
                    now,
                    self._owner_id,
                    claim_hash,
                    lease_expires_at,
                    canonical_flow_id,
                    OAuthFlowState.PENDING.value,
                ),
            )
            material = _exchange_material_from_row(row)
        return OAuthCompletionClaim(
            flow_id=canonical_flow_id,
            claim_token=claim_token,
            material=material,
            lease_expires_at=lease_expires_at,
        )

    def device_poll_material(self, flow_id: str) -> OAuthExchangeMaterial:
        """Return device polling material without consuming the pending flow.

        This method is for the trusted backend exchange adapter only.  Keeping
        the flow pending while a user authorizes the device means ``cancel``
        and expiry remain effective during a potentially long poll loop.
        """

        canonical_flow_id = _validate_flow_id(flow_id)
        self.recover(flow_id=canonical_flow_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_flows WHERE flow_id = ?", (canonical_flow_id,)
            ).fetchone()
        if row is None:
            raise OAuthFlowNotFoundError("OAuth flow was not found.")
        if OAuthFlowState(str(row["state"])) is not OAuthFlowState.PENDING:
            raise OAuthFlowStateError("OAuth device flow is not pending authorization.")
        if OAuthFlowKind(str(row["kind"])) is not OAuthFlowKind.DEVICE_CODE:
            raise OAuthFlowStateError("OAuth flow does not use device authorization.")
        return _exchange_material_from_row(row)

    def finish_completion(
        self,
        claim: OAuthCompletionClaim,
        tokens: OAuthTokenSet,
        *,
        nonce: str | None = None,
    ) -> OAuthFlowStatus:
        canonical_flow_id = _validate_flow_id(claim.flow_id)
        if not isinstance(tokens, OAuthTokenSet):
            raise OAuthValidationError("OAuth token result is invalid.")
        row = self._consume_live_claim(canonical_flow_id, claim.claim_token, returned_nonce=nonce)
        server_id = str(row["server_id"])
        try:
            self._vault_store(server_id, tokens, binding_id=canonical_flow_id)
        except OAuthVaultError:
            self._fail_consumed_claim(canonical_flow_id, error_code="vault_store_failed")
            raise

        now = self._now()
        with self._transaction() as connection:
            live = connection.execute(
                "SELECT state, error_code, completion_owner, completion_claim_hash FROM oauth_flows "
                "WHERE flow_id = ?",
                (canonical_flow_id,),
            ).fetchone()
            interrupted = bool(
                live is not None
                and OAuthFlowState(str(live["state"])) is OAuthFlowState.FAILED
                and str(live["error_code"] or "") == "completion_interrupted"
            )
            if not interrupted and (
                live is None
                or OAuthFlowState(str(live["state"])) is not OAuthFlowState.COMPLETING
                or str(live["completion_owner"] or "") != self._owner_id
                or live["completion_claim_hash"] is not None
            ):
                # A successful deterministic binding is recoverable, but this
                # claim is still single-use and cannot be finalized twice.
                raise OAuthFlowStateError("OAuth completion claim is no longer active.")
            self._set_terminal(
                connection,
                canonical_flow_id,
                OAuthFlowState.COMPLETED,
                now=now,
                error_code=None,
            )
        return self.status(canonical_flow_id)

    def complete(
        self,
        flow_id: str,
        tokens: OAuthTokenSet,
        *,
        state: str | None = None,
        nonce: str | None = None,
    ) -> OAuthFlowStatus:
        """Claim and finish a flow when token exchange has already completed."""

        self._validate_token_nonce_preflight(flow_id, nonce=nonce)
        claim = self.begin_completion(flow_id, state=state, nonce=nonce)
        return self.finish_completion(claim, tokens, nonce=nonce)

    def fail_completion(
        self, claim: OAuthCompletionClaim, *, error_code: str = "token_exchange_failed"
    ) -> OAuthFlowStatus:
        safe_error_code = _validate_error_code(error_code)
        self._fail_live_claim(claim.flow_id, claim.claim_token, error_code=safe_error_code)
        return self.status(claim.flow_id)

    def fail_pending(
        self, flow_id: str, *, error_code: str = "authorization_failed"
    ) -> OAuthFlowStatus:
        """Fail a pending flow without accepting provider-controlled error text."""

        canonical_flow_id = _validate_flow_id(flow_id)
        safe_error_code = _validate_error_code(error_code)
        self.recover(flow_id=canonical_flow_id)
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM oauth_flows WHERE flow_id = ?", (canonical_flow_id,)
            ).fetchone()
            if row is None:
                raise OAuthFlowNotFoundError("OAuth flow was not found.")
            if OAuthFlowState(str(row["state"])) is not OAuthFlowState.PENDING:
                raise OAuthFlowStateError("OAuth flow is not pending authorization.")
            self._set_terminal(
                connection,
                canonical_flow_id,
                OAuthFlowState.FAILED,
                now=now,
                error_code=safe_error_code,
            )
            return self._status_in_transaction(connection, canonical_flow_id)

    def cancel(self, flow_id: str) -> OAuthFlowStatus:
        canonical_flow_id = _validate_flow_id(flow_id)
        self.recover(flow_id=canonical_flow_id)
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM oauth_flows WHERE flow_id = ?", (canonical_flow_id,)
            ).fetchone()
            if row is None:
                raise OAuthFlowNotFoundError("OAuth flow was not found.")
            current = OAuthFlowState(str(row["state"]))
            if current is OAuthFlowState.CANCELLED:
                return self._status_in_transaction(connection, canonical_flow_id)
            if current is not OAuthFlowState.PENDING:
                raise OAuthFlowStateError("OAuth flow can no longer be cancelled.")
            self._set_terminal(
                connection,
                canonical_flow_id,
                OAuthFlowState.CANCELLED,
                now=now,
                error_code="cancelled_by_user",
            )
            return self._status_in_transaction(connection, canonical_flow_id)

    def logout(
        self,
        server_id: str,
        *,
        revoker: Callable[[OAuthTokenSet], None] | None = None,
    ) -> OAuthLogoutResult:
        canonical_server_id = _validate_server_id(server_id)
        self.recover()
        tokens = self._vault_load(canonical_server_id)

        revocation_attempted = revoker is not None and tokens is not None
        revocation_succeeded: bool | None = None
        error_code: str | None = None
        if revocation_attempted:
            try:
                assert revoker is not None and tokens is not None
                revoker(tokens)
                revocation_succeeded = True
            except Exception:  # noqa: BLE001 - never surface provider/token details
                revocation_succeeded = False
                error_code = "remote_revocation_failed"

        removed = self._vault_delete(canonical_server_id)

        now = self._now()
        with self._transaction() as connection:
            active = connection.execute(
                """
                SELECT COUNT(*) AS count
                  FROM oauth_flows
                 WHERE server_id = ? AND state IN (?, ?)
                """,
                (
                    canonical_server_id,
                    OAuthFlowState.PENDING.value,
                    OAuthFlowState.COMPLETING.value,
                ),
            ).fetchone()
            cancelled = int(active["count"] if active is not None else 0)
            connection.execute(
                """
                UPDATE oauth_flows
                   SET state = ?, updated_at = ?, terminal_at = ?, error_code = ?,
                       state_hash = NULL, nonce_hash = NULL, nonce_value = NULL,
                       pkce_verifier = NULL,
                       device_code = NULL, completion_owner = NULL,
                       completion_claim_hash = NULL, completion_lease_expires_at = NULL,
                       authorization_url = NULL, user_code = NULL,
                       verification_uri_complete = NULL
                 WHERE server_id = ? AND state IN (?, ?)
                """,
                (
                    OAuthFlowState.CANCELLED.value,
                    now,
                    now,
                    "logout",
                    canonical_server_id,
                    OAuthFlowState.PENDING.value,
                    OAuthFlowState.COMPLETING.value,
                ),
            )
        return OAuthLogoutResult(
            server_id=canonical_server_id,
            local_credentials_removed=removed,
            active_flows_cancelled=cancelled,
            remote_revocation_attempted=revocation_attempted,
            remote_revocation_succeeded=revocation_succeeded,
            error_code=error_code,
        )

    def recover(self, *, flow_id: str | None = None) -> int:
        """Recover interrupted completion and expire overdue flows.

        A vault binding proves that credentials were atomically stored before a
        crash.  Otherwise a completing flow remains leased until its fencing
        deadline and then fails closed rather than retrying a possibly consumed
        authorization code.
        """

        canonical_flow_id = _validate_flow_id(flow_id) if flow_id is not None else None
        now = self._now()
        with self._connect() as connection:
            if canonical_flow_id is None:
                rows = connection.execute(
                    "SELECT flow_id, server_id, state, expires_at, completion_lease_expires_at "
                    "FROM oauth_flows WHERE state IN (?, ?) "
                    "OR (state = ? AND error_code = ?)",
                    (
                        OAuthFlowState.PENDING.value,
                        OAuthFlowState.COMPLETING.value,
                        OAuthFlowState.FAILED.value,
                        "completion_interrupted",
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT flow_id, server_id, state, expires_at, completion_lease_expires_at "
                    "FROM oauth_flows WHERE flow_id = ? AND "
                    "(state IN (?, ?) OR (state = ? AND error_code = ?))",
                    (
                        canonical_flow_id,
                        OAuthFlowState.PENDING.value,
                        OAuthFlowState.COMPLETING.value,
                        OAuthFlowState.FAILED.value,
                        "completion_interrupted",
                    ),
                ).fetchall()

        recovered = 0
        for row in rows:
            current = OAuthFlowState(str(row["state"]))
            target: OAuthFlowState | None = None
            error_code: str | None = None
            if current is OAuthFlowState.PENDING and now >= float(row["expires_at"]):
                target = OAuthFlowState.EXPIRED
                error_code = "flow_expired"
            elif current is OAuthFlowState.COMPLETING:
                binding_exists = self._vault_has_binding(
                    str(row["server_id"]), binding_id=str(row["flow_id"])
                )
                if binding_exists:
                    target = OAuthFlowState.COMPLETED
                else:
                    lease_expiry = float(row["completion_lease_expires_at"] or 0.0)
                    if now >= min(float(row["expires_at"]), lease_expiry):
                        target = OAuthFlowState.FAILED
                        error_code = "completion_interrupted"
            elif current is OAuthFlowState.FAILED:
                binding_exists = self._vault_has_binding(
                    str(row["server_id"]), binding_id=str(row["flow_id"])
                )
                if binding_exists:
                    target = OAuthFlowState.COMPLETED
                    error_code = None
            if target is None:
                continue
            with self._transaction() as connection:
                live = connection.execute(
                    "SELECT state FROM oauth_flows WHERE flow_id = ?", (str(row["flow_id"]),)
                ).fetchone()
                if live is None or OAuthFlowState(str(live["state"])) is not current:
                    continue
                self._set_terminal(
                    connection,
                    str(row["flow_id"]),
                    target,
                    now=now,
                    error_code=error_code,
                )
                recovered += 1
        return recovered

    def cleanup(
        self, *, terminal_retention_seconds: float = DEFAULT_TERMINAL_RETENTION_SECONDS
    ) -> int:
        retention = _bounded_duration(
            terminal_retention_seconds,
            field_name="terminal retention",
            minimum=0.0,
            maximum=365 * 24 * 60 * 60.0,
        )
        self.recover()
        cutoff = self._now() - retention
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM oauth_flows
                 WHERE state IN (?, ?, ?, ?) AND terminal_at IS NOT NULL AND terminal_at <= ?
                """,
                (
                    OAuthFlowState.COMPLETED.value,
                    OAuthFlowState.CANCELLED.value,
                    OAuthFlowState.FAILED.value,
                    OAuthFlowState.EXPIRED.value,
                    cutoff,
                ),
            )
            return max(0, int(cursor.rowcount))

    def _prepare_storage(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _restrict_permissions(self.path.parent, directory=True)
            _secure_precreate_database(self.path)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_info (
                        version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS oauth_flows (
                        flow_id TEXT PRIMARY KEY,
                        server_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        authorization_url TEXT,
                        token_endpoint TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        redirect_uri TEXT,
                        scopes_json TEXT NOT NULL,
                        state_hash TEXT,
                        nonce_hash TEXT,
                        nonce_value TEXT,
                        nonce_required INTEGER NOT NULL DEFAULT 0,
                        pkce_verifier TEXT,
                        device_code TEXT,
                        verification_uri TEXT,
                        verification_uri_complete TEXT,
                        user_code TEXT,
                        polling_interval REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        terminal_at REAL,
                        error_code TEXT,
                        completion_owner TEXT,
                        completion_claim_hash TEXT,
                        completion_lease_expires_at REAL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS oauth_flows_one_active_server
                        ON oauth_flows(server_id)
                        WHERE state IN ('pending', 'completing');
                    CREATE INDEX IF NOT EXISTS oauth_flows_terminal_cleanup
                        ON oauth_flows(state, terminal_at);
                    """
                )
                row = connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,)
                    )
                elif int(row["version"]) != SCHEMA_VERSION:
                    raise OAuthLifecycleError("OAuth flow registry schema is unsupported.")
            _restrict_permissions(self.path, directory=False)
        except OAuthLifecycleError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OAuthLifecycleError("OAuth flow registry could not be initialized.") from exc

    def _vault_store(self, server_id: str, tokens: OAuthTokenSet, *, binding_id: str) -> None:
        failed = False
        try:
            self.vault.store(server_id, tokens, binding_id=binding_id)
        except Exception:  # noqa: BLE001 - erase secret-bearing exception context
            failed = True
        if failed:
            raise OAuthVaultError("OAuth credentials could not be stored securely.")

    def _vault_load(self, server_id: str) -> OAuthTokenSet | None:
        failed = False
        tokens: OAuthTokenSet | None = None
        try:
            tokens = self.vault.load(server_id)
        except Exception:  # noqa: BLE001 - erase secret-bearing exception context
            failed = True
        if failed:
            raise OAuthVaultError("OAuth credentials could not be read securely.")
        return tokens

    def _vault_has_binding(self, server_id: str, *, binding_id: str) -> bool:
        failed = False
        result = False
        try:
            result = bool(self.vault.has_binding(server_id, binding_id=binding_id))
        except Exception:  # noqa: BLE001 - erase secret-bearing exception context
            failed = True
        if failed:
            raise OAuthVaultError("OAuth credential recovery check failed.")
        return result

    def _vault_delete(self, server_id: str) -> bool:
        failed = False
        removed = False
        try:
            removed = bool(self.vault.delete(server_id))
        except Exception:  # noqa: BLE001 - erase secret-bearing exception context
            failed = True
        if failed:
            raise OAuthVaultError("OAuth credentials could not be removed securely.")
        return removed

    def _insert_flow(self, row: Mapping[str, Any]) -> None:
        columns = tuple(row)
        placeholders = ", ".join("?" for _ in columns)
        try:
            with self._transaction() as connection:
                connection.execute(
                    f"INSERT INTO oauth_flows ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
                    tuple(row[column] for column in columns),
                )
        except sqlite3.IntegrityError as exc:
            raise OAuthFlowConflictError(
                "An OAuth flow is already active for this server."
            ) from exc
        except OAuthLifecycleError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OAuthLifecycleError("OAuth flow could not be persisted.") from exc

    def _consume_live_claim(
        self, flow_id: str, claim_token: str, *, returned_nonce: str | None
    ) -> sqlite3.Row:
        now = self._now()
        with self._transaction() as connection:
            row = self._select_claim_for_update(connection, flow_id, claim_token, now=now)
            if row is None:
                raise OAuthFlowStateError("OAuth completion claim is no longer active.")
            kind = OAuthFlowKind(str(row["kind"]))
            if kind is OAuthFlowKind.AUTHORIZATION_CODE:
                nonce_required = bool(int(row["nonce_required"] or 0))
                expected_nonce_hash = str(row["nonce_hash"] or "")
                if returned_nonce is None:
                    if nonce_required:
                        raise OAuthCompletionRejectedError("OAuth token nonce proof is incomplete.")
                elif not hmac.compare_digest(expected_nonce_hash, _secret_digest(returned_nonce)):
                    raise OAuthCompletionRejectedError("OAuth token nonce proof was rejected.")
            elif returned_nonce is not None:
                raise OAuthCompletionRejectedError(
                    "Device-code completion does not accept a token nonce."
                )
            connection.execute(
                "UPDATE oauth_flows SET completion_claim_hash = NULL, updated_at = ? "
                "WHERE flow_id = ?",
                (now, flow_id),
            )
            return row

    def _select_claim_for_update(
        self,
        connection: sqlite3.Connection,
        flow_id: str,
        claim_token: str,
        *,
        now: float,
    ) -> sqlite3.Row | None:
        if not isinstance(claim_token, str) or not claim_token:
            return None
        row = connection.execute(
            "SELECT * FROM oauth_flows WHERE flow_id = ?", (flow_id,)
        ).fetchone()
        if row is None or OAuthFlowState(str(row["state"])) is not OAuthFlowState.COMPLETING:
            return None
        if str(row["completion_owner"] or "") != self._owner_id:
            return None
        expected = str(row["completion_claim_hash"] or "")
        if not hmac.compare_digest(expected, _secret_digest(claim_token)):
            return None
        if now >= float(row["completion_lease_expires_at"] or 0.0):
            return None
        return row

    def _fail_live_claim(self, flow_id: str, claim_token: str, *, error_code: str) -> None:
        canonical_flow_id = _validate_flow_id(flow_id)
        now = self._now()
        with self._transaction() as connection:
            row = self._select_claim_for_update(connection, canonical_flow_id, claim_token, now=now)
            if row is None:
                raise OAuthFlowStateError("OAuth completion claim is no longer active.")
            self._set_terminal(
                connection,
                canonical_flow_id,
                OAuthFlowState.FAILED,
                now=now,
                error_code=error_code,
            )

    def _fail_consumed_claim(self, flow_id: str, *, error_code: str) -> None:
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state, completion_owner, completion_claim_hash FROM oauth_flows "
                "WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
            if (
                row is None
                or OAuthFlowState(str(row["state"])) is not OAuthFlowState.COMPLETING
                or str(row["completion_owner"] or "") != self._owner_id
                or row["completion_claim_hash"] is not None
            ):
                raise OAuthFlowStateError("OAuth completion claim is no longer active.")
            self._set_terminal(
                connection,
                flow_id,
                OAuthFlowState.FAILED,
                now=now,
                error_code=error_code,
            )

    def _validate_callback_proof(
        self, row: sqlite3.Row, *, state: str | None, nonce: str | None
    ) -> None:
        if not isinstance(state, str):
            raise OAuthCompletionRejectedError("OAuth callback proof is incomplete.")
        expected_state = str(row["state_hash"] or "")
        expected_nonce = str(row["nonce_hash"] or "")
        state_valid = hmac.compare_digest(expected_state, _secret_digest(state))
        nonce_valid = nonce is None or hmac.compare_digest(expected_nonce, _secret_digest(nonce))
        if not state_valid or not nonce_valid:
            raise OAuthCompletionRejectedError("OAuth callback proof was rejected.")

    def _validate_token_nonce_preflight(self, flow_id: str, *, nonce: str | None) -> None:
        canonical_flow_id = _validate_flow_id(flow_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT kind, nonce_hash, nonce_required FROM oauth_flows WHERE flow_id = ?",
                (canonical_flow_id,),
            ).fetchone()
        if row is None:
            raise OAuthFlowNotFoundError("OAuth flow was not found.")
        if OAuthFlowKind(str(row["kind"])) is OAuthFlowKind.DEVICE_CODE:
            if nonce is not None:
                raise OAuthCompletionRejectedError(
                    "Device-code completion does not accept a token nonce."
                )
            return
        nonce_required = bool(int(row["nonce_required"] or 0))
        if nonce is None:
            if nonce_required:
                raise OAuthCompletionRejectedError("OAuth token nonce proof is incomplete.")
            return
        if not hmac.compare_digest(str(row["nonce_hash"] or ""), _secret_digest(nonce)):
            raise OAuthCompletionRejectedError("OAuth token nonce proof was rejected.")

    def _set_terminal(
        self,
        connection: sqlite3.Connection,
        flow_id: str,
        state: OAuthFlowState,
        *,
        now: float,
        error_code: str | None,
    ) -> None:
        if state not in TERMINAL_STATES:
            raise OAuthLifecycleError("Internal OAuth terminal transition is invalid.")
        connection.execute(
            """
            UPDATE oauth_flows
               SET state = ?, updated_at = ?, terminal_at = ?, error_code = ?,
                   state_hash = NULL, nonce_hash = NULL, nonce_value = NULL,
                   pkce_verifier = NULL,
                   device_code = NULL, completion_owner = NULL,
                   completion_claim_hash = NULL, completion_lease_expires_at = NULL,
                   authorization_url = NULL, user_code = NULL,
                   verification_uri_complete = NULL
             WHERE flow_id = ?
            """,
            (state.value, now, now, error_code, flow_id),
        )

    def _status_in_transaction(
        self, connection: sqlite3.Connection, flow_id: str
    ) -> OAuthFlowStatus:
        row = connection.execute(
            "SELECT * FROM oauth_flows WHERE flow_id = ?", (flow_id,)
        ).fetchone()
        if row is None:
            raise OAuthFlowNotFoundError("OAuth flow was not found.")
        return _status_from_row(row)

    def _new_flow_id(self) -> str:
        for _ in range(8):
            candidate = str(self._id_factory())
            if _FLOW_ID_RE.fullmatch(candidate):
                return candidate
        raise OAuthLifecycleError("Could not allocate an opaque OAuth flow id.")

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise OAuthLifecycleError("OAuth lifecycle clock returned an invalid value.")
        return float(value)

    def _validate_storage_location(self, workspace_roots: Iterable[str | os.PathLike[str]]) -> None:
        candidate = self.path.expanduser().resolve(strict=False)
        for raw_root in workspace_roots:
            root = Path(raw_root).expanduser().resolve(strict=False)
            if candidate == root or root in candidate.parents:
                raise OAuthValidationError("OAuth flow registry must be outside the workspace.")

    @contextmanager
    def _connect(self) -> Any:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 10000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Any:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()


def _status_from_row(row: sqlite3.Row) -> OAuthFlowStatus:
    return OAuthFlowStatus(
        flow_id=str(row["flow_id"]),
        server_id=str(row["server_id"]),
        kind=OAuthFlowKind(str(row["kind"])),
        state=OAuthFlowState(str(row["state"])),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        expires_at=float(row["expires_at"]),
        authorization_url=_optional_row_string(row, "authorization_url"),
        verification_uri=_optional_row_string(row, "verification_uri"),
        verification_uri_complete=_optional_row_string(row, "verification_uri_complete"),
        user_code=_optional_row_string(row, "user_code"),
        polling_interval=(
            float(row["polling_interval"]) if row["polling_interval"] is not None else None
        ),
        terminal_at=float(row["terminal_at"]) if row["terminal_at"] is not None else None,
        error_code=_optional_row_string(row, "error_code"),
    )


def _exchange_material_from_row(row: sqlite3.Row) -> OAuthExchangeMaterial:
    return OAuthExchangeMaterial(
        flow_id=str(row["flow_id"]),
        server_id=str(row["server_id"]),
        kind=OAuthFlowKind(str(row["kind"])),
        token_endpoint=str(row["token_endpoint"]),
        client_id=str(row["client_id"]),
        redirect_uri=_optional_row_string(row, "redirect_uri"),
        pkce_verifier=_optional_row_string(row, "pkce_verifier"),
        expected_nonce=_optional_row_string(row, "nonce_value"),
        device_code=_optional_row_string(row, "device_code"),
        scopes=_decode_scopes(str(row["scopes_json"])),
    )


def _optional_row_string(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    return str(value) if value is not None else None


def _validate_server_id(value: object) -> str:
    if not isinstance(value, str) or not _SERVER_ID_RE.fullmatch(value.strip()):
        raise OAuthValidationError("OAuth server id is invalid.")
    return value.strip()


def _validate_flow_id(value: object) -> str:
    if not isinstance(value, str) or not _FLOW_ID_RE.fullmatch(value):
        raise OAuthValidationError("OAuth flow id is invalid.")
    return value


def _validate_client_id(value: object) -> str:
    return _required_public_string(value, field_name="OAuth client id", maximum=1024)


def _required_secret(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 8192:
        raise OAuthValidationError(f"OAuth {field_name} is invalid.")
    return value


def _required_public_string(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise OAuthValidationError(f"{field_name} is invalid.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or any(ord(char) < 0x20 for char in cleaned):
        raise OAuthValidationError(f"{field_name} is invalid.")
    return cleaned


def _normalize_scopes(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise OAuthValidationError("OAuth scopes must be a collection.")
    try:
        raw_values = tuple(values)
    except TypeError as exc:
        raise OAuthValidationError("OAuth scopes must be a collection.") from exc
    if len(raw_values) > MAX_SCOPES:
        raise OAuthValidationError("OAuth scope count exceeds the allowed limit.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if not isinstance(value, str) or not _SCOPE_RE.fullmatch(value):
            raise OAuthValidationError("OAuth scope is invalid.")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _normalize_extra_params(values: Mapping[str, JsonScalar]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise OAuthValidationError("OAuth authorization parameters must be an object.")
    if len(values) > 32:
        raise OAuthValidationError("OAuth authorization parameter count exceeds the limit.")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        if not isinstance(raw_name, str) or not _PARAM_NAME_RE.fullmatch(raw_name):
            raise OAuthValidationError("OAuth authorization parameter name is invalid.")
        name = raw_name.lower()
        if name in _RESERVED_AUTH_PARAMS:
            raise OAuthValidationError("OAuth authorization parameter overrides a reserved field.")
        if name in _SENSITIVE_URL_PARAM_NAMES:
            raise OAuthValidationError("OAuth authorization parameter may expose credentials.")
        if raw_value is None:
            continue
        if isinstance(raw_value, bool):
            value = "true" if raw_value else "false"
        elif isinstance(raw_value, (str, int, float)):
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                raise OAuthValidationError("OAuth authorization parameter value is invalid.")
            value = str(raw_value)
        else:
            raise OAuthValidationError("OAuth authorization parameter value is invalid.")
        if len(value) > 1024 or any(ord(char) < 0x20 for char in value):
            raise OAuthValidationError("OAuth authorization parameter value is invalid.")
        normalized[raw_name] = value
    return normalized


def _validate_https_url(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise OAuthValidationError(f"OAuth {field_name} is invalid.")
    try:
        split = urlsplit(value)
        port = split.port
    except ValueError as exc:
        raise OAuthValidationError(f"OAuth {field_name} is invalid.") from exc
    if (
        split.scheme.lower() != "https"
        or not split.hostname
        or split.username is not None
        or split.password is not None
        or split.fragment
        or port is not None
        and (port < 1 or port > 65535)
    ):
        raise OAuthValidationError(f"OAuth {field_name} must be a secure HTTPS URL.")
    if any(
        name.lower() in _SENSITIVE_URL_PARAM_NAMES
        for name, _ in parse_qsl(split.query, keep_blank_values=True)
    ):
        raise OAuthValidationError(f"OAuth {field_name} may expose credentials.")
    return urlunsplit(("https", split.netloc.lower(), split.path or "/", split.query, ""))


def _validate_redirect_uri(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise OAuthValidationError("OAuth redirect URI is invalid.")
    try:
        split = urlsplit(value)
        port = split.port
    except ValueError as exc:
        raise OAuthValidationError("OAuth redirect URI is invalid.") from exc
    host = str(split.hostname or "").lower()
    if (
        split.scheme.lower() != "http"
        or host not in {"127.0.0.1", "::1"}
        or split.username is not None
        or split.password is not None
        or split.fragment
        or split.query
        or port is None
        or port < 1024
        or port > 65535
        or not split.path.startswith("/")
    ):
        raise OAuthValidationError("OAuth redirect URI must be an allowlisted loopback URL.")
    normalized_host = f"[{host}]" if ":" in host else host
    return urlunsplit(("http", f"{normalized_host}:{port}", split.path or "/", "", ""))


def _url_origin(value: str) -> tuple[str, str, int | None]:
    split = urlsplit(value)
    return split.scheme.lower(), str(split.hostname or "").lower(), split.port


def _build_authorization_url(
    endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str,
    nonce: str,
    code_challenge: str,
    extras: Mapping[str, str],
) -> str:
    split = urlsplit(endpoint)
    existing = parse_qsl(split.query, keep_blank_values=True)
    if any(name.lower() in _RESERVED_AUTH_PARAMS for name, _ in existing):
        raise OAuthValidationError("OAuth authorization endpoint contains reserved parameters.")
    params = [*existing, *extras.items()]
    params.extend(
        [
            ("response_type", "code"),
            ("client_id", client_id),
            ("redirect_uri", redirect_uri),
            ("scope", " ".join(scopes)),
            ("state", state),
            ("nonce", nonce),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
        ]
    )
    result = urlunsplit((split.scheme, split.netloc, split.path, urlencode(params), ""))
    if len(result) > MAX_URL_LENGTH:
        raise OAuthValidationError("OAuth authorization URL exceeds the allowed length.")
    return result


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_scopes(scopes: tuple[str, ...]) -> str:
    return json.dumps(list(scopes), ensure_ascii=True, separators=(",", ":"))


def _decode_scopes(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise OAuthLifecycleError("OAuth flow registry contains invalid scope metadata.") from exc
    return _normalize_scopes(payload)


def _validate_error_code(value: object) -> str:
    if not isinstance(value, str) or not _ERROR_CODE_RE.fullmatch(value):
        raise OAuthValidationError("OAuth error code is invalid.")
    return value


def _bounded_duration(value: object, *, field_name: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
        or float(value) > maximum
    ):
        raise OAuthValidationError(f"OAuth {field_name} is outside the allowed range.")
    return float(value)


def _restrict_permissions(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError as exc:
        raise OAuthLifecycleError("OAuth flow registry permissions could not be secured.") from exc


def _secure_precreate_database(path: Path) -> None:
    if path.is_symlink():
        raise OAuthLifecycleError("OAuth flow registry cannot use a symbolic link.")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise OAuthLifecycleError("OAuth flow registry cannot use a symbolic link.") from None
        return
    except OSError as exc:
        raise OAuthLifecycleError("OAuth flow registry could not be created securely.") from exc
    else:
        os.close(descriptor)
