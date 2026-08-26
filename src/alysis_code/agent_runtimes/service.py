from __future__ import annotations

from dataclasses import dataclass

from ..config import AgentRuntimeSettings, AppConfig, ConfigError
from .base import (
    RuntimeAccountStatus,
    RuntimeProbeStatus,
    RuntimeTurnRequest,
    RuntimeTurnResult,
)
from .builtins import (
    RuntimeSetupOption,
    create_runtime_registry,
    runtime_setup_option,
)
from .registry import UnknownRuntimeError


class RuntimeConnectionError(ConfigError):
    """Raised when a configured delegated runtime cannot be resolved."""


@dataclass(frozen=True, slots=True)
class RuntimeConnectionSnapshot:
    option: RuntimeSetupOption
    settings: AgentRuntimeSettings
    probe: RuntimeProbeStatus
    account: RuntimeAccountStatus


def resolve_runtime_id(cfg: AppConfig, runtime_id: str | None = None) -> str:
    resolved = str(runtime_id or cfg.execution.runtime or "").strip()
    if not resolved:
        raise RuntimeConnectionError("No delegated agent runtime is selected.")
    return resolved


def ensure_runtime_settings(cfg: AppConfig, runtime_id: str) -> AgentRuntimeSettings:
    normalized = str(runtime_id or "").strip()
    option = runtime_setup_option(normalized)
    if option is None:
        raise RuntimeConnectionError(f"Unknown delegated agent runtime: {normalized or '(empty)'}.")
    settings = cfg.agent_runtimes.get(normalized)
    if settings is None:
        settings = AgentRuntimeSettings(
            adapter=option.adapter,
            executable=option.default_executable,
        )
        cfg.agent_runtimes[normalized] = settings
    return settings


def activate_runtime(cfg: AppConfig, runtime_id: str) -> AgentRuntimeSettings:
    settings = ensure_runtime_settings(cfg, runtime_id)
    cfg.execution.backend = "delegated"
    cfg.execution.runtime = str(runtime_id).strip()
    return settings


def activate_native_runtime(cfg: AppConfig) -> None:
    cfg.execution.backend = "native"
    cfg.execution.runtime = None


def runtime_connection_snapshot(
    cfg: AppConfig,
    runtime_id: str | None = None,
) -> RuntimeConnectionSnapshot:
    resolved = resolve_runtime_id(cfg, runtime_id)
    option = runtime_setup_option(resolved)
    if option is None:
        raise RuntimeConnectionError(f"Unknown delegated agent runtime: {resolved}.")
    settings = ensure_runtime_settings(cfg, resolved)
    try:
        adapter = create_runtime_registry().get(resolved)
    except UnknownRuntimeError as exc:
        raise RuntimeConnectionError(str(exc)) from exc
    probe = adapter.probe(settings)
    account = (
        adapter.account_status(settings)
        if probe.available
        else RuntimeAccountStatus(
            authenticated=False,
            verified=False,
            detail=probe.detail,
        )
    )
    return RuntimeConnectionSnapshot(
        option=option,
        settings=settings,
        probe=probe,
        account=account,
    )


def login_runtime(
    cfg: AppConfig,
    runtime_id: str | None = None,
    *,
    method_id: str,
) -> RuntimeAccountStatus:
    resolved = resolve_runtime_id(cfg, runtime_id)
    settings = ensure_runtime_settings(cfg, resolved)
    try:
        adapter = create_runtime_registry().get(resolved)
    except UnknownRuntimeError as exc:
        raise RuntimeConnectionError(str(exc)) from exc
    return adapter.login(settings, method_id)


def logout_runtime(
    cfg: AppConfig,
    runtime_id: str | None = None,
) -> RuntimeAccountStatus:
    resolved = resolve_runtime_id(cfg, runtime_id)
    settings = ensure_runtime_settings(cfg, resolved)
    try:
        adapter = create_runtime_registry().get(resolved)
    except UnknownRuntimeError as exc:
        raise RuntimeConnectionError(str(exc)) from exc
    return adapter.logout(settings)


def run_runtime_turn(
    cfg: AppConfig,
    request: RuntimeTurnRequest,
    runtime_id: str | None = None,
) -> RuntimeTurnResult:
    resolved = resolve_runtime_id(cfg, runtime_id)
    settings = ensure_runtime_settings(cfg, resolved)
    try:
        adapter = create_runtime_registry().get(resolved)
    except UnknownRuntimeError as exc:
        raise RuntimeConnectionError(str(exc)) from exc
    return adapter.run_turn(settings, request)


__all__ = [
    "RuntimeConnectionError",
    "RuntimeConnectionSnapshot",
    "activate_native_runtime",
    "activate_runtime",
    "ensure_runtime_settings",
    "login_runtime",
    "logout_runtime",
    "resolve_runtime_id",
    "run_runtime_turn",
    "runtime_connection_snapshot",
]
