from __future__ import annotations

import copy
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import config_path
from ..mcp.token_store import (
    load_token_payload_result,
    migrate_legacy_token_payload,
    save_token_payload,
)
from .base import ProviderAuthError

_STORE_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class ProviderTokenRecord:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str | None = None
    account_label: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> ProviderTokenRecord:
        if not isinstance(value, dict):
            raise ProviderAuthError("Stored provider credentials are invalid; connect again.")
        access_token = str(value.get("access_token") or "").strip()
        refresh_token = str(value.get("refresh_token") or "").strip()
        try:
            expires_at = float(value.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ProviderAuthError(
                "Stored provider credentials are invalid; connect again."
            ) from exc
        if not access_token or not refresh_token or expires_at <= 0:
            raise ProviderAuthError("Stored provider credentials are incomplete; connect again.")
        account_id = str(value.get("account_id") or "").strip() or None
        account_label = str(value.get("account_label") or "").strip() or None
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            account_id=account_id,
            account_label=account_label,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provider_token_store_path() -> Path:
    return config_path().parent / "provider_auth_tokens.json"


def load_provider_token(provider_id: str) -> ProviderTokenRecord | None:
    normalized = _provider_id(provider_id)
    with _STORE_LOCK:
        try:
            payload = _load_provider_payload(provider_token_store_path())
        except Exception as exc:  # noqa: BLE001 - secret-store backends vary by OS
            raise ProviderAuthError(
                "Could not read the encrypted provider credential store."
            ) from exc
        value = payload.get(normalized)
        if value is None:
            return None
        return ProviderTokenRecord.from_dict(copy.deepcopy(value))


def save_provider_token(provider_id: str, record: ProviderTokenRecord) -> None:
    normalized = _provider_id(provider_id)
    with _STORE_LOCK:
        path = provider_token_store_path()
        try:
            payload = _load_provider_payload(path)
            payload[normalized] = record.to_dict()
            save_token_payload(path, payload)
        except Exception as exc:  # noqa: BLE001 - never leak token values through backend errors
            raise ProviderAuthError(
                "Could not save credentials in the encrypted provider store."
            ) from exc


def delete_provider_token(provider_id: str) -> bool:
    normalized = _provider_id(provider_id)
    with _STORE_LOCK:
        path = provider_token_store_path()
        try:
            payload = _load_provider_payload(path)
            removed = payload.pop(normalized, None) is not None
            if removed:
                save_token_payload(path, payload)
            return removed
        except Exception as exc:  # noqa: BLE001
            raise ProviderAuthError(
                "Could not update the encrypted provider credential store."
            ) from exc


def _provider_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ProviderAuthError("Provider id cannot be empty.")
    return normalized


def _load_provider_payload(path: Path) -> dict[str, Any]:
    result = load_token_payload_result(path)
    if result.legacy_plaintext:
        migrate_legacy_token_payload(path, result.payload)
    return result.payload


__all__ = [
    "ProviderTokenRecord",
    "delete_provider_token",
    "load_provider_token",
    "provider_token_store_path",
    "save_provider_token",
]
