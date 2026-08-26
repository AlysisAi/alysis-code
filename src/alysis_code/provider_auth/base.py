from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class ProviderAuthError(RuntimeError):
    """A provider account could not be connected or authorized safely."""


class ProviderLoginRequiredError(ProviderAuthError):
    """The selected provider account must be connected again."""


@dataclass(frozen=True, slots=True)
class ProviderAccountStatus:
    connected: bool
    verified: bool = True
    account_label: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderReasoningEffort:
    id: str
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    label: str
    description: str = ""
    is_default: bool = False
    reasoning_efforts: tuple[ProviderReasoningEffort, ...] = ()
    default_reasoning_effort: str | None = None
    input_modalities: tuple[str, ...] = ("text",)
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderAuthSetupOption:
    id: str
    label: str
    description: str
    auth_hint: str = ""


@runtime_checkable
class ProviderAuthAdapter(Protocol):
    """Native model-transport auth owned by a provider-specific adapter."""

    provider_id: str
    display_name: str
    description: str
    profile_name: str
    auth_hint: str
    base_url: str
    protocol: str
    supports_previous_response_id: bool
    supports_temperature: bool
    requires_streaming: bool

    def account_status(self) -> ProviderAccountStatus: ...

    def login(
        self,
        method: str = "browser",
        *,
        output_write: Callable[[str], None] | None = None,
    ) -> ProviderAccountStatus: ...

    def logout(self) -> ProviderAccountStatus: ...

    def list_models(self, *, refresh: bool = False) -> tuple[ProviderModel, ...]: ...

    def authorization_headers(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
    ) -> Mapping[str, str]: ...

    def adapt_responses_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


__all__ = [
    "ProviderAccountStatus",
    "ProviderAuthAdapter",
    "ProviderAuthError",
    "ProviderAuthSetupOption",
    "ProviderLoginRequiredError",
    "ProviderModel",
    "ProviderReasoningEffort",
]
