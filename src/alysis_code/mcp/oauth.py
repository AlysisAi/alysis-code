from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import SplitResult, urlencode, urlsplit, urlunsplit

import httpx

from ..safety import SafeHttpError, safe_http_request
from .errors import McpAuthError, McpConfigError
from .models import validate_http_url, validate_https_url
from .oauth_store import McpOAuthTokenRecord, McpOAuthTokenStoreError

__all__ = [
    "McpAuthorizationServerMetadata",
    "McpOAuthAuthRequiredError",
    "McpOAuthCallbackError",
    "McpOAuthConfigError",
    "McpOAuthDiscoveryError",
    "McpOAuthError",
    "McpOAuthInsufficientScopeError",
    "McpOAuthReLoginRequired",
    "McpOAuthTokenExchangeError",
    "McpOAuthTokenStoreError",
    "McpProtectedResourceMetadata",
    "_redact_token",
    "build_authorization_url",
    "build_pkce_challenge",
    "canonical_mcp_resource_uri",
    "discover_authorization_server_metadata",
    "discover_authorization_server_metadata_from_url",
    "discover_protected_resource_metadata",
    "exchange_authorization_code",
    "generate_oauth_state",
    "generate_pkce_verifier",
    "is_token_expired",
    "parse_www_authenticate_bearer_challenge",
    "refresh_access_token",
    "resolve_requested_scopes",
]

_PKCE_VERIFIER_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-\._~]{43,128}$")
_BEARER_PARAM_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"')


class McpOAuthError(McpAuthError):
    def __init__(
        self,
        message: str,
        *,
        server_id: str,
        authorization_server_url: str | None = None,
    ) -> None:
        self.server_id = str(server_id).strip()
        self.authorization_server_origin = _url_origin(authorization_server_url)
        prefix = f"{self.__class__.__name__}: server '{self.server_id}'"
        if self.authorization_server_origin:
            prefix += f" auth server '{self.authorization_server_origin}'"
        cleaned = str(message or "OAuth operation failed.").strip()
        super().__init__(f"{prefix}: {cleaned}", server_id=self.server_id)


class McpOAuthConfigError(McpOAuthError, McpConfigError):
    error_code = "mcp_oauth_config_error"


class McpOAuthDiscoveryError(McpOAuthError):
    pass


class McpOAuthAuthRequiredError(McpOAuthError):
    pass


class McpOAuthReLoginRequired(McpOAuthError):
    pass


class McpOAuthInsufficientScopeError(McpOAuthError):
    pass


class McpOAuthCallbackError(McpOAuthError):
    pass


class McpOAuthTokenExchangeError(McpOAuthError):
    pass


class _McpOAuthDiscoveryLookupError(McpOAuthDiscoveryError):
    pass


@dataclass(frozen=True)
class McpProtectedResourceMetadata:
    authorization_servers: tuple[str, ...]
    resource: str | None = None
    scopes_supported: tuple[str, ...] = field(default_factory=tuple)
    raw_payload: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class McpAuthorizationServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    code_challenge_methods_supported: tuple[str, ...] = field(default_factory=tuple)
    response_types_supported: tuple[str, ...] = field(default_factory=tuple)
    scopes_supported: tuple[str, ...] = field(default_factory=tuple)
    raw_payload: dict[str, Any] = field(repr=False, default_factory=dict)


def _redact_token(value: str | None) -> str:
    if not value:
        return "[redacted:0chars]"
    return f"[redacted:{len(value)}chars]"


def _url_origin(value: str | None) -> str | None:
    if not value:
        return None
    split = urlsplit(value)
    if not split.scheme or not split.netloc:
        return None
    netloc = split.netloc
    return urlunsplit(SplitResult(split.scheme, netloc, "", "", ""))


def canonical_mcp_resource_uri(server_url: str) -> str:
    normalized_url = validate_http_url(server_url, field_name="resource_server_url")
    split = urlsplit(normalized_url)
    hostname = str(split.hostname or "").strip()
    if not hostname:
        raise McpOAuthConfigError(
            "resource_server_url must include a valid host.",
            server_id="oauth",
            authorization_server_url=normalized_url,
        )
    normalized_scheme = split.scheme.lower()
    normalized_host = hostname.lower()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    netloc = normalized_host
    if split.port is not None:
        netloc = f"{netloc}:{split.port}"
    path = split.path or ""
    normalized_path = "" if path == "/" else path
    return urlunsplit(SplitResult(normalized_scheme, netloc, normalized_path, "", ""))


def _required_string(
    value: object,
    *,
    field_name: str,
    server_id: str,
    authorization_server_url: str | None = None,
    error_cls: type[McpOAuthError] = McpOAuthDiscoveryError,
) -> str:
    if not isinstance(value, str):
        raise error_cls(
            f"field '{field_name}' must be a string.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    cleaned = value.strip()
    if not cleaned:
        raise error_cls(
            f"field '{field_name}' cannot be empty.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    return cleaned


def _optional_string(
    value: object,
    *,
    field_name: str,
    server_id: str,
    authorization_server_url: str | None = None,
    error_cls: type[McpOAuthError] = McpOAuthDiscoveryError,
) -> str | None:
    if value is None:
        return None
    return _required_string(
        value,
        field_name=field_name,
        server_id=server_id,
        authorization_server_url=authorization_server_url,
        error_cls=error_cls,
    )


def _normalize_string_list(
    value: object,
    *,
    field_name: str,
    server_id: str,
    authorization_server_url: str | None = None,
    error_cls: type[McpOAuthError] = McpOAuthDiscoveryError,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise error_cls(
            f"field '{field_name}' must be an array of strings.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    normalized: list[str] = []
    for item in value:
        normalized.append(
            _required_string(
                item,
                field_name=field_name,
                server_id=server_id,
                authorization_server_url=authorization_server_url,
                error_cls=error_cls,
            )
        )
    return tuple(normalized)


def _normalize_scope_list(
    value: object,
    *,
    field_name: str,
    server_id: str,
    authorization_server_url: str | None = None,
    error_cls: type[McpOAuthError] = McpOAuthDiscoveryError,
) -> tuple[str, ...]:
    return _normalize_requested_scopes(
        _normalize_string_list(
            value,
            field_name=field_name,
            server_id=server_id,
            authorization_server_url=authorization_server_url,
            error_cls=error_cls,
        )
    )


def _require_json_object(
    payload: object,
    *,
    context: str,
    server_id: str,
    authorization_server_url: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise McpOAuthDiscoveryError(
            f"{context} must be a JSON object.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    return payload


def _fetch_json(
    *,
    url: str,
    server_id: str,
    timeout_s: float,
    authorization_server_url: str | None = None,
) -> dict[str, Any]:
    try:
        response = asyncio.run(
            safe_http_request(
                "GET",
                url,
                timeout=timeout_s,
                headers={"Accept": "application/json"},
            )
        )
        response.raise_for_status()
    except SafeHttpError as exc:
        raise _McpOAuthDiscoveryLookupError(
            f"request blocked for '{url}': {exc}",
            server_id=server_id,
            authorization_server_url=authorization_server_url or url,
        ) from exc
    except httpx.HTTPError as exc:
        raise _McpOAuthDiscoveryLookupError(
            f"request failed for '{url}'.",
            server_id=server_id,
            authorization_server_url=authorization_server_url or url,
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise McpOAuthDiscoveryError(
            f"response from '{url}' was not valid JSON.",
            server_id=server_id,
            authorization_server_url=authorization_server_url or url,
        ) from exc
    return _require_json_object(
        payload,
        context=f"discovery document '{url}'",
        server_id=server_id,
        authorization_server_url=authorization_server_url or url,
    )


def parse_www_authenticate_bearer_challenge(header_value: str | None) -> dict[str, str]:
    text = str(header_value or "").strip()
    if not text:
        return {}
    lowered = text.lower()
    bearer_index = lowered.find("bearer")
    if bearer_index < 0:
        return {}
    bearer_segment = text[bearer_index + len("Bearer") :]
    params: dict[str, str] = {}
    for match in _BEARER_PARAM_RE.finditer(bearer_segment):
        params[match.group(1)] = match.group(2)
    return params


def _normalize_protected_resource_metadata(
    payload: dict[str, Any],
    *,
    server_id: str,
) -> McpProtectedResourceMetadata:
    resource = _optional_string(payload.get("resource"), field_name="resource", server_id=server_id)
    authorization_servers = _normalize_string_list(
        payload.get("authorization_servers"),
        field_name="authorization_servers",
        server_id=server_id,
    )
    if not authorization_servers:
        raise McpOAuthDiscoveryError(
            "protected resource metadata must include authorization_servers.",
            server_id=server_id,
        )
    normalized_servers = tuple(
        validate_https_url(url, field_name="authorization_servers[]", allow_loopback_http=True)
        for url in authorization_servers
    )
    return McpProtectedResourceMetadata(
        authorization_servers=normalized_servers,
        resource=resource,
        scopes_supported=_normalize_scope_list(
            payload.get("scopes_supported"),
            field_name="scopes_supported",
            server_id=server_id,
        ),
        raw_payload=dict(payload),
    )


def _normalize_authorization_server_metadata(
    payload: dict[str, Any],
    *,
    server_id: str,
    authorization_server_url: str,
) -> McpAuthorizationServerMetadata:
    issuer = validate_https_url(
        _required_string(
            payload.get("issuer"),
            field_name="issuer",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        ),
        field_name="issuer",
        allow_loopback_http=True,
    )
    authorization_endpoint = validate_https_url(
        _required_string(
            payload.get("authorization_endpoint"),
            field_name="authorization_endpoint",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        ),
        field_name="authorization_endpoint",
        allow_loopback_http=True,
    )
    token_endpoint = validate_https_url(
        _required_string(
            payload.get("token_endpoint"),
            field_name="token_endpoint",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        ),
        field_name="token_endpoint",
        allow_loopback_http=True,
    )
    return McpAuthorizationServerMetadata(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        code_challenge_methods_supported=_normalize_string_list(
            payload.get("code_challenge_methods_supported"),
            field_name="code_challenge_methods_supported",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        ),
        response_types_supported=_normalize_string_list(
            payload.get("response_types_supported"),
            field_name="response_types_supported",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        ),
        scopes_supported=_normalize_scope_list(
            payload.get("scopes_supported"),
            field_name="scopes_supported",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        ),
        raw_payload=dict(payload),
    )


def _authorization_server_metadata_urls(authorization_server_url: str) -> tuple[str, ...]:
    split = urlsplit(authorization_server_url)
    origin = urlunsplit(SplitResult(split.scheme, split.netloc, "", "", ""))
    if split.path and split.path != "/":
        appended_path = split.path.rstrip("/") + "/.well-known/openid-configuration"
        return (
            f"{origin}/.well-known/oauth-authorization-server{split.path}",
            f"{origin}/.well-known/openid-configuration{split.path}",
            f"{origin}{appended_path}",
        )
    return (
        f"{origin}/.well-known/oauth-authorization-server",
        f"{origin}/.well-known/openid-configuration",
    )


def _protected_resource_metadata_urls(resource_server_url: str) -> tuple[str, ...]:
    split = urlsplit(resource_server_url)
    origin = urlunsplit(SplitResult(split.scheme, split.netloc, "", "", ""))
    urls: list[str] = []
    if split.path and split.path != "/":
        urls.append(f"{origin}/.well-known/oauth-protected-resource{split.path}")
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return tuple(urls)


def discover_protected_resource_metadata(
    *,
    server_id: str,
    resource_server_url: str,
    unauthorized_response: httpx.Response | None = None,
    timeout_s: float = 10.0,
    client: httpx.Client | None = None,
) -> McpProtectedResourceMetadata:
    del client
    normalized_resource_url = validate_http_url(
        resource_server_url, field_name="resource_server_url"
    )
    challenge_params = parse_www_authenticate_bearer_challenge(
        unauthorized_response.headers.get("WWW-Authenticate")
        if unauthorized_response is not None
        else None
    )
    resource_metadata_url = challenge_params.get("resource_metadata")
    if resource_metadata_url is not None:
        normalized_metadata_url = validate_https_url(
            resource_metadata_url,
            field_name="resource_metadata",
            allow_loopback_http=True,
        )
        payload = _fetch_json(
            url=normalized_metadata_url,
            server_id=server_id,
            timeout_s=timeout_s,
        )
        return _normalize_protected_resource_metadata(payload, server_id=server_id)
    last_lookup_error: _McpOAuthDiscoveryLookupError | None = None
    for metadata_url in _protected_resource_metadata_urls(normalized_resource_url):
        try:
            payload = _fetch_json(
                url=metadata_url,
                server_id=server_id,
                timeout_s=timeout_s,
            )
        except _McpOAuthDiscoveryLookupError as exc:
            last_lookup_error = exc
            continue
        return _normalize_protected_resource_metadata(payload, server_id=server_id)
    raise McpOAuthDiscoveryError(
        "protected resource metadata discovery failed via path-specific and root well-known fallbacks.",
        server_id=server_id,
        authorization_server_url=normalized_resource_url,
    ) from last_lookup_error


def discover_authorization_server_metadata_from_url(
    *,
    server_id: str,
    authorization_server_url: str,
    timeout_s: float = 10.0,
    client: httpx.Client | None = None,
) -> McpAuthorizationServerMetadata:
    del client
    normalized_auth_server_url = validate_https_url(
        authorization_server_url,
        field_name="authorization_server_url",
        allow_loopback_http=True,
    )
    last_lookup_error: _McpOAuthDiscoveryLookupError | None = None
    for metadata_url in _authorization_server_metadata_urls(normalized_auth_server_url):
        try:
            payload = _fetch_json(
                url=metadata_url,
                server_id=server_id,
                timeout_s=timeout_s,
                authorization_server_url=normalized_auth_server_url,
            )
        except _McpOAuthDiscoveryLookupError as exc:
            last_lookup_error = exc
            continue
        return _normalize_authorization_server_metadata(
            payload,
            server_id=server_id,
            authorization_server_url=normalized_auth_server_url,
        )
    raise McpOAuthDiscoveryError(
        "authorization server discovery failed via RFC 8414 and OIDC fallbacks.",
        server_id=server_id,
        authorization_server_url=normalized_auth_server_url,
    ) from last_lookup_error


def discover_authorization_server_metadata(
    *,
    server_id: str,
    resource_server_url: str,
    unauthorized_response: httpx.Response | None = None,
    authorization_server_url: str | None = None,
    timeout_s: float = 10.0,
    client: httpx.Client | None = None,
) -> McpAuthorizationServerMetadata:
    del client
    if authorization_server_url is not None:
        return discover_authorization_server_metadata_from_url(
            server_id=server_id,
            authorization_server_url=authorization_server_url,
            timeout_s=timeout_s,
        )
    protected_metadata = discover_protected_resource_metadata(
        server_id=server_id,
        resource_server_url=resource_server_url,
        unauthorized_response=unauthorized_response,
        timeout_s=timeout_s,
    )
    return discover_authorization_server_metadata_from_url(
        server_id=server_id,
        authorization_server_url=protected_metadata.authorization_servers[0],
        timeout_s=timeout_s,
    )


def generate_pkce_verifier(length: int = 64) -> str:
    if not 43 <= int(length) <= 128:
        raise McpOAuthConfigError(
            "PKCE verifier length must be between 43 and 128 characters.",
            server_id="pkce",
        )
    return "".join(secrets.choice(_PKCE_VERIFIER_CHARS) for _ in range(int(length)))


def build_pkce_challenge(verifier: str) -> str:
    if not isinstance(verifier, str) or not _PKCE_VERIFIER_RE.fullmatch(verifier):
        raise McpOAuthConfigError(
            "PKCE verifier must be 43-128 RFC 3986 unreserved characters.",
            server_id="pkce",
        )
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def generate_oauth_state() -> str:
    return secrets.token_hex(32)


def _normalize_scope_string(scope_value: str) -> tuple[str, ...]:
    parts = [part.strip() for part in scope_value.split(" ")]
    normalized: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part or part in seen:
            continue
        seen.add(part)
        normalized.append(part)
    return tuple(normalized)


def _normalize_requested_scopes(scopes: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_scope in list(scopes or []):
        cleaned = str(raw_scope or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return tuple(normalized)


def resolve_requested_scopes(
    *,
    configured_scopes: tuple[str, ...] | list[str] | None,
    challenge_scope: str | None,
    metadata_scopes_supported: tuple[str, ...] | list[str] | None,
    existing_granted_scopes: tuple[str, ...] | list[str] | None,
    purpose: Literal["login", "refresh"],
) -> tuple[str, ...]:
    configured = _normalize_requested_scopes(configured_scopes)
    if configured:
        return configured
    if purpose == "login":
        challenge = _normalize_scope_string(challenge_scope) if challenge_scope else ()
        if challenge:
            return challenge
        metadata_scopes = _normalize_requested_scopes(metadata_scopes_supported)
        if metadata_scopes:
            return metadata_scopes
        return ()
    if purpose == "refresh":
        granted = _normalize_requested_scopes(existing_granted_scopes)
        if granted:
            return granted
        return ()
    raise ValueError(f"unsupported scope resolution purpose: {purpose}")


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_expires_in(
    value: object,
    *,
    server_id: str,
    authorization_server_url: str,
) -> int:
    if isinstance(value, bool):
        raise McpOAuthTokenExchangeError(
            "token response field 'expires_in' must be a non-negative integer.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    if isinstance(value, int):
        expires_in = value
    elif isinstance(value, float) and value.is_integer():
        expires_in = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        expires_in = int(value.strip())
    else:
        raise McpOAuthTokenExchangeError(
            "token response field 'expires_in' must be a non-negative integer.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    if expires_in < 0:
        raise McpOAuthTokenExchangeError(
            "token response field 'expires_in' must be a non-negative integer.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    return expires_in


def _normalize_token_response(
    payload: object,
    *,
    server_id: str,
    authorization_server_url: str,
    requested_scopes: tuple[str, ...] | list[str] | None,
    existing_granted_scopes: tuple[str, ...] | list[str] | None = None,
    existing_refresh_token: str | None = None,
    obtained_at: datetime | None = None,
) -> McpOAuthTokenRecord:
    if not isinstance(payload, dict):
        raise McpOAuthTokenExchangeError(
            "token response must be a JSON object.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    access_token = _required_string(
        payload.get("access_token"),
        field_name="access_token",
        server_id=server_id,
        authorization_server_url=authorization_server_url,
        error_cls=McpOAuthTokenExchangeError,
    )
    token_type = _required_string(
        payload.get("token_type"),
        field_name="token_type",
        server_id=server_id,
        authorization_server_url=authorization_server_url,
        error_cls=McpOAuthTokenExchangeError,
    )
    if token_type.casefold() != "bearer":
        raise McpOAuthTokenExchangeError(
            "token response field 'token_type' must be 'Bearer'.",
            server_id=server_id,
            authorization_server_url=authorization_server_url,
        )
    expires_in = _parse_expires_in(
        payload.get("expires_in"),
        server_id=server_id,
        authorization_server_url=authorization_server_url,
    )
    response_scope = _optional_string(
        payload.get("scope"),
        field_name="scope",
        server_id=server_id,
        authorization_server_url=authorization_server_url,
        error_cls=McpOAuthTokenExchangeError,
    )
    refresh_token = _optional_string(
        payload.get("refresh_token"),
        field_name="refresh_token",
        server_id=server_id,
        authorization_server_url=authorization_server_url,
        error_cls=McpOAuthTokenExchangeError,
    )
    if refresh_token is None:
        refresh_token = existing_refresh_token
    normalized_requested_scopes = _normalize_requested_scopes(requested_scopes)
    normalized_existing_granted_scopes = _normalize_requested_scopes(existing_granted_scopes)
    if response_scope is not None:
        granted_scopes = _normalize_scope_string(response_scope)
    elif normalized_requested_scopes:
        granted_scopes = normalized_requested_scopes
    elif normalized_existing_granted_scopes:
        granted_scopes = normalized_existing_granted_scopes
    else:
        granted_scopes = ()
    recorded_at = (obtained_at or _utc_now()).astimezone(UTC).replace(microsecond=0)
    return McpOAuthTokenRecord(
        access_token=access_token,
        token_type=token_type,
        expires_at=recorded_at + timedelta(seconds=expires_in),
        refresh_token=refresh_token,
        granted_scopes=granted_scopes,
        obtained_at=recorded_at,
    )


def is_token_expired(record: McpOAuthTokenRecord, *, now: datetime | None = None) -> bool:
    reference = (now or _utc_now()).astimezone(UTC).replace(microsecond=0)
    return reference >= record.expires_at


def build_authorization_url(
    *,
    server_id: str,
    authorization_server_metadata: McpAuthorizationServerMetadata,
    resource_server_url: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: tuple[str, ...] | list[str] | None = None,
) -> str:
    normalized_redirect_uri = validate_http_url(redirect_uri, field_name="redirect_uri")
    normalized_client_id = _required_string(
        client_id,
        field_name="client_id",
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.authorization_endpoint,
        error_cls=McpOAuthConfigError,
    )
    normalized_state = _required_string(
        state,
        field_name="state",
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.authorization_endpoint,
        error_cls=McpOAuthConfigError,
    )
    normalized_challenge = _required_string(
        code_challenge,
        field_name="code_challenge",
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.authorization_endpoint,
        error_cls=McpOAuthConfigError,
    )
    params = {
        "response_type": "code",
        "client_id": normalized_client_id,
        "redirect_uri": normalized_redirect_uri,
        "resource": canonical_mcp_resource_uri(resource_server_url),
        "code_challenge": normalized_challenge,
        "code_challenge_method": "S256",
        "state": normalized_state,
    }
    normalized_scopes = _normalize_requested_scopes(scopes)
    if normalized_scopes:
        params["scope"] = " ".join(normalized_scopes)
    return f"{authorization_server_metadata.authorization_endpoint}?{urlencode(params)}"


def _post_token_request(
    *,
    server_id: str,
    token_endpoint: str,
    payload: dict[str, str],
    timeout_s: float,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    del client
    encoded_payload = urlencode(payload).encode("utf-8")
    try:
        response = asyncio.run(
            safe_http_request(
                "POST",
                token_endpoint,
                timeout=timeout_s,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=encoded_payload,
            )
        )
    except httpx.TimeoutException as exc:
        raise McpOAuthTokenExchangeError(
            "token endpoint request timed out.",
            server_id=server_id,
            authorization_server_url=token_endpoint,
        ) from exc
    except SafeHttpError as exc:
        raise McpOAuthTokenExchangeError(
            f"token endpoint request blocked: {exc}",
            server_id=server_id,
            authorization_server_url=token_endpoint,
        ) from exc
    except httpx.HTTPError as exc:
        raise McpOAuthTokenExchangeError(
            "token endpoint request failed.",
            server_id=server_id,
            authorization_server_url=token_endpoint,
        ) from exc
    if response.status_code >= 400:
        raise McpOAuthTokenExchangeError(
            f"token endpoint returned status {response.status_code}.",
            server_id=server_id,
            authorization_server_url=token_endpoint,
        )
    try:
        return _require_json_object(
            response.json(),
            context="token response",
            server_id=server_id,
            authorization_server_url=token_endpoint,
        )
    except McpOAuthDiscoveryError as exc:
        raise McpOAuthTokenExchangeError(
            "token response must be a JSON object.",
            server_id=server_id,
            authorization_server_url=token_endpoint,
        ) from exc
    except ValueError as exc:
        raise McpOAuthTokenExchangeError(
            "token response was not valid JSON.",
            server_id=server_id,
            authorization_server_url=token_endpoint,
        ) from exc


def exchange_authorization_code(
    *,
    server_id: str,
    authorization_server_metadata: McpAuthorizationServerMetadata,
    resource_server_url: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    requested_scopes: tuple[str, ...] | list[str] | None = None,
    timeout_s: float = 10.0,
    client: httpx.Client | None = None,
) -> McpOAuthTokenRecord:
    normalized_code = _required_string(
        code,
        field_name="code",
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.token_endpoint,
        error_cls=McpOAuthTokenExchangeError,
    )
    normalized_client_id = _required_string(
        client_id,
        field_name="client_id",
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.token_endpoint,
        error_cls=McpOAuthTokenExchangeError,
    )
    normalized_redirect_uri = validate_http_url(redirect_uri, field_name="redirect_uri")
    build_pkce_challenge(code_verifier)
    token_payload = _post_token_request(
        server_id=server_id,
        token_endpoint=authorization_server_metadata.token_endpoint,
        payload={
            "grant_type": "authorization_code",
            "code": normalized_code,
            "redirect_uri": normalized_redirect_uri,
            "client_id": normalized_client_id,
            "resource": canonical_mcp_resource_uri(resource_server_url),
            "code_verifier": code_verifier,
        },
        timeout_s=timeout_s,
        client=client,
    )
    return _normalize_token_response(
        token_payload,
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.token_endpoint,
        requested_scopes=requested_scopes,
    )


def refresh_access_token(
    *,
    server_id: str,
    authorization_server_metadata: McpAuthorizationServerMetadata,
    resource_server_url: str,
    client_id: str,
    refresh_token: str,
    requested_scopes: tuple[str, ...] | list[str] | None = None,
    existing_granted_scopes: tuple[str, ...] | list[str] | None = None,
    timeout_s: float = 10.0,
    client: httpx.Client | None = None,
) -> McpOAuthTokenRecord:
    normalized_refresh_token = _required_string(
        refresh_token,
        field_name="refresh_token",
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.token_endpoint,
        error_cls=McpOAuthTokenExchangeError,
    )
    normalized_client_id = _required_string(
        client_id,
        field_name="client_id",
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.token_endpoint,
        error_cls=McpOAuthTokenExchangeError,
    )
    normalized_requested_scopes = _normalize_requested_scopes(requested_scopes)
    token_request_payload = {
        "grant_type": "refresh_token",
        "refresh_token": normalized_refresh_token,
        "client_id": normalized_client_id,
        "resource": canonical_mcp_resource_uri(resource_server_url),
    }
    if normalized_requested_scopes:
        token_request_payload["scope"] = " ".join(normalized_requested_scopes)
    token_payload = _post_token_request(
        server_id=server_id,
        token_endpoint=authorization_server_metadata.token_endpoint,
        payload=token_request_payload,
        timeout_s=timeout_s,
        client=client,
    )
    return _normalize_token_response(
        token_payload,
        server_id=server_id,
        authorization_server_url=authorization_server_metadata.token_endpoint,
        requested_scopes=requested_scopes,
        existing_granted_scopes=existing_granted_scopes,
        existing_refresh_token=normalized_refresh_token,
    )
