from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import user_mcp_config_path
from .errors import (
    McpOAuthTokenStoreError,
    McpTokenStoreCorruptError,
    McpTokenStoreMigrationError,
    McpTokenStoreUnavailableError,
    McpTokenStoreVersionError,
)
from .models import normalize_ordered_string_list, normalize_server_id
from .token_store import load_token_payload_result, migrate_legacy_token_payload, save_token_payload

__all__ = [
    "McpOAuthTokenRecord",
    "McpOAuthTokenStoreError",
    "McpTokenStoreCorruptError",
    "McpTokenStoreMigrationError",
    "McpTokenStoreUnavailableError",
    "McpTokenStoreVersionError",
    "delete_oauth_token_record",
    "list_oauth_token_server_ids",
    "load_oauth_token_record",
    "mcp_oauth_token_store_path",
    "save_oauth_token_record",
]

_OAUTH_BINDING_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _store_path() -> Path:
    return user_mcp_config_path().with_name("mcp_oauth_tokens.json")


def mcp_oauth_token_store_path() -> Path:
    return _store_path()


def _canonical_server_id(server_id: str) -> str:
    try:
        return normalize_server_id(server_id)
    except Exception as exc:  # noqa: BLE001
        raise McpOAuthTokenStoreError(
            f"Invalid OAuth token store server id: {server_id!r}"
        ) from exc


def _require_string(value: object, *, field_name: str, server_id: str) -> str:
    if not isinstance(value, str):
        raise McpOAuthTokenStoreError(
            f"OAuth token store entry for server '{server_id}' field '{field_name}' must be a string."
        )
    cleaned = value.strip()
    if not cleaned:
        raise McpOAuthTokenStoreError(
            f"OAuth token store entry for server '{server_id}' field '{field_name}' cannot be empty."
        )
    return cleaned


def _optional_string(value: object, *, field_name: str, server_id: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name=field_name, server_id=server_id)


def _optional_binding_id(value: object, *, server_id: str) -> str | None:
    if value is None:
        return None
    binding_id = _require_string(value, field_name="binding_id", server_id=server_id)
    if not _OAUTH_BINDING_ID_RE.fullmatch(binding_id):
        raise McpOAuthTokenStoreError(
            f"OAuth token store entry for server '{server_id}' field 'binding_id' is invalid."
        )
    return binding_id


def _normalize_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise McpOAuthTokenStoreError(f"OAuth token field '{field_name}' must be a datetime.")
    if value.tzinfo is None:
        raise McpOAuthTokenStoreError(
            f"OAuth token field '{field_name}' must be timezone-aware UTC."
        )
    return value.astimezone(UTC).replace(microsecond=0)


def _parse_datetime(value: object, *, field_name: str, server_id: str) -> datetime:
    raw = _require_string(value, field_name=field_name, server_id=server_id)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise McpOAuthTokenStoreError(
            f"OAuth token store entry for server '{server_id}' field '{field_name}' must be ISO 8601 UTC."
        ) from exc
    if parsed.tzinfo is None:
        raise McpOAuthTokenStoreError(
            f"OAuth token store entry for server '{server_id}' field '{field_name}' must be timezone-aware UTC."
        )
    return parsed.astimezone(UTC).replace(microsecond=0)


def _format_datetime(value: datetime) -> str:
    return _normalize_datetime(value, field_name="timestamp").isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class McpOAuthTokenRecord:
    access_token: str = field(repr=False)
    token_type: str
    expires_at: datetime
    refresh_token: str | None = field(default=None, repr=False)
    granted_scopes: tuple[str, ...] = field(default_factory=tuple)
    obtained_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))
    binding_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        access_token = _require_string(
            self.access_token, field_name="access_token", server_id="<record>"
        )
        token_type = _require_string(self.token_type, field_name="token_type", server_id="<record>")
        refresh_token = _optional_string(
            self.refresh_token,
            field_name="refresh_token",
            server_id="<record>",
        )
        scopes = tuple(
            normalize_ordered_string_list(
                list(self.granted_scopes),
                field_name="granted_scopes",
            )
        )
        expires_at = _normalize_datetime(self.expires_at, field_name="expires_at")
        obtained_at = _normalize_datetime(self.obtained_at, field_name="obtained_at")
        binding_id = _optional_binding_id(self.binding_id, server_id="<record>")
        object.__setattr__(self, "access_token", access_token)
        object.__setattr__(self, "token_type", token_type)
        object.__setattr__(self, "refresh_token", refresh_token)
        object.__setattr__(self, "granted_scopes", scopes)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "obtained_at", obtained_at)
        object.__setattr__(self, "binding_id", binding_id)

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_at": _format_datetime(self.expires_at),
            "refresh_token": self.refresh_token,
            "granted_scopes": list(self.granted_scopes),
            "obtained_at": _format_datetime(self.obtained_at),
        }
        if self.binding_id is not None:
            payload["binding_id"] = self.binding_id
        return payload

    @classmethod
    def from_payload(cls, payload: object, *, server_id: str) -> McpOAuthTokenRecord:
        if not isinstance(payload, dict):
            raise McpOAuthTokenStoreError(
                f"OAuth token store entry for server '{server_id}' must be an object."
            )
        scopes_raw = payload.get("granted_scopes")
        try:
            granted_scopes = tuple(
                normalize_ordered_string_list(scopes_raw, field_name="granted_scopes")
            )
        except (TypeError, ValueError) as exc:
            raise McpOAuthTokenStoreError(
                f"OAuth token store entry for server '{server_id}' field 'granted_scopes' is invalid."
            ) from exc
        return cls(
            access_token=_require_string(
                payload.get("access_token"),
                field_name="access_token",
                server_id=server_id,
            ),
            token_type=_require_string(
                payload.get("token_type"),
                field_name="token_type",
                server_id=server_id,
            ),
            expires_at=_parse_datetime(
                payload.get("expires_at"),
                field_name="expires_at",
                server_id=server_id,
            ),
            refresh_token=_optional_string(
                payload.get("refresh_token"),
                field_name="refresh_token",
                server_id=server_id,
            ),
            granted_scopes=granted_scopes,
            obtained_at=_parse_datetime(
                payload.get("obtained_at"),
                field_name="obtained_at",
                server_id=server_id,
            ),
            binding_id=_optional_binding_id(payload.get("binding_id"), server_id=server_id),
        )


def _load_store_payload() -> tuple[dict[str, Any], bool]:
    path = _store_path()
    result = load_token_payload_result(path)
    payload = result.payload
    if not isinstance(payload, dict):
        raise McpOAuthTokenStoreError(f"Invalid OAuth token store format: {path}")
    normalized: dict[str, Any] = {}
    for raw_server_id, record in payload.items():
        if not isinstance(raw_server_id, str):
            raise McpOAuthTokenStoreError(f"Invalid OAuth token store server id in: {path}")
        normalized[_canonical_server_id(raw_server_id)] = record
    return normalized, result.legacy_plaintext


def _migrate_after_successful_legacy_read(
    payload: dict[str, Any], *, legacy_plaintext: bool
) -> None:
    if legacy_plaintext:
        migrate_legacy_token_payload(_store_path(), payload)


def load_oauth_token_record(server_id: str) -> McpOAuthTokenRecord | None:
    canonical_server_id = _canonical_server_id(server_id)
    payload, legacy_plaintext = _load_store_payload()
    record = payload.get(canonical_server_id)
    if record is None:
        _migrate_after_successful_legacy_read(payload, legacy_plaintext=legacy_plaintext)
        return None
    loaded = McpOAuthTokenRecord.from_payload(record, server_id=canonical_server_id)
    _migrate_after_successful_legacy_read(payload, legacy_plaintext=legacy_plaintext)
    return loaded


def save_oauth_token_record(server_id: str, record: McpOAuthTokenRecord) -> None:
    canonical_server_id = _canonical_server_id(server_id)
    path = _store_path()
    payload, _legacy_plaintext = _load_store_payload()
    payload[canonical_server_id] = record.as_payload()
    try:
        save_token_payload(path, payload)
    except McpOAuthTokenStoreError as exc:
        raise McpOAuthTokenStoreError(
            f"Failed to write OAuth token store entry for server '{canonical_server_id}' to {path}"
        ) from exc


def delete_oauth_token_record(server_id: str) -> bool:
    canonical_server_id = _canonical_server_id(server_id)
    payload, legacy_plaintext = _load_store_payload()
    if canonical_server_id not in payload:
        _migrate_after_successful_legacy_read(payload, legacy_plaintext=legacy_plaintext)
        return False
    del payload[canonical_server_id]
    path = _store_path()
    try:
        save_token_payload(path, payload)
    except McpOAuthTokenStoreError as exc:
        raise McpOAuthTokenStoreError(
            f"Failed to delete OAuth token store entry for server '{canonical_server_id}' from {path}"
        ) from exc
    return True


def list_oauth_token_server_ids() -> tuple[str, ...]:
    payload, legacy_plaintext = _load_store_payload()
    _migrate_after_successful_legacy_read(payload, legacy_plaintext=legacy_plaintext)
    return tuple(sorted(payload))
