from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig, ConfigError, resolve_model_metadata_policy
from .model_registry import (
    DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS,
    DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
    ModelRegistry,
)

_CAPACITY_FIELDS: tuple[str, ...] = ("context_window_tokens", "max_output_tokens")


class ModelMetadataPolicyError(ConfigError):
    pass


@dataclass(frozen=True)
class ActiveModelRef:
    role: str
    model_name: str


@dataclass(frozen=True)
class ActiveModelMetadataDiagnostic:
    role: str
    model_name: str
    resolved_model_name: str
    source: str
    field_sources: dict[str, str]
    warnings: tuple[str, ...]
    last_registry_error: str | None
    fallback_capacity_fields: tuple[str, ...]
    fallback_capacity_active: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model_name,
            "resolved_model_name": self.resolved_model_name,
            "source": self.source,
            "field_sources": dict(self.field_sources),
            "warnings": list(self.warnings),
            "last_registry_error": self.last_registry_error,
            "fallback_capacity_fields": list(self.fallback_capacity_fields),
            "fallback_capacity_active": self.fallback_capacity_active,
        }


@dataclass(frozen=True)
class ActiveModelMetadataPolicyResult:
    policy: str
    diagnostics: tuple[ActiveModelMetadataDiagnostic, ...]
    warning_messages: tuple[str, ...]


def _fallback_capacity_fields(field_sources: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        field_name
        for field_name in _CAPACITY_FIELDS
        if str(field_sources.get(field_name) or "") == "fallback"
    )


def collect_active_model_metadata_diagnostics(
    *,
    registry: ModelRegistry,
    active_models: list[ActiveModelRef],
) -> tuple[ActiveModelMetadataDiagnostic, ...]:
    diagnostics: list[ActiveModelMetadataDiagnostic] = []
    cached: dict[str, ActiveModelMetadataDiagnostic] = {}
    for active in active_models:
        model_name = active.model_name.strip()
        role = active.role.strip().lower() or "unknown"
        if not model_name:
            continue
        cache_key = model_name.casefold()
        cached_diag = cached.get(cache_key)
        if cached_diag is None:
            meta = registry.get(model_name)
            field_sources = dict(meta.field_sources)
            fallback_fields = _fallback_capacity_fields(field_sources)
            cached_diag = ActiveModelMetadataDiagnostic(
                role=role,
                model_name=model_name,
                resolved_model_name=str(meta.model_name or model_name),
                source=str(meta.source or "unknown"),
                field_sources=field_sources,
                warnings=tuple(meta.warnings),
                last_registry_error=registry.last_error,
                fallback_capacity_fields=fallback_fields,
                fallback_capacity_active=bool(fallback_fields),
            )
            cached[cache_key] = cached_diag
        diagnostics.append(
            ActiveModelMetadataDiagnostic(
                role=role,
                model_name=cached_diag.model_name,
                resolved_model_name=cached_diag.resolved_model_name,
                source=cached_diag.source,
                field_sources=dict(cached_diag.field_sources),
                warnings=tuple(cached_diag.warnings),
                last_registry_error=cached_diag.last_registry_error,
                fallback_capacity_fields=tuple(cached_diag.fallback_capacity_fields),
                fallback_capacity_active=cached_diag.fallback_capacity_active,
            )
        )
    return tuple(diagnostics)


def _group_diagnostics_for_warnings(
    diagnostics: tuple[ActiveModelMetadataDiagnostic, ...],
) -> list[tuple[str, list[str], tuple[str, ...], str | None]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for diagnostic in diagnostics:
        if not diagnostic.fallback_capacity_active:
            continue
        key = diagnostic.model_name.casefold()
        if key not in grouped:
            grouped[key] = {
                "model_name": diagnostic.model_name,
                "roles": [],
                "fallback_capacity_fields": diagnostic.fallback_capacity_fields,
                "last_registry_error": diagnostic.last_registry_error,
            }
            order.append(key)
        roles = grouped[key]["roles"]
        if diagnostic.role not in roles:
            roles.append(diagnostic.role)
    return [
        (
            str(grouped[key]["model_name"]),
            list(grouped[key]["roles"]),
            tuple(grouped[key]["fallback_capacity_fields"]),
            grouped[key]["last_registry_error"],
        )
        for key in order
    ]


def _format_fix_hint() -> str:
    return (
        "Set model_metadata_overrides, use ALYSIS_CONTEXT_WINDOW/ALYSIS_MAX_OUTPUT_TOKENS, "
        "or choose a known model."
    )


def _format_warning_message(
    *,
    model_name: str,
    roles: list[str],
    fallback_capacity_fields: tuple[str, ...],
    last_registry_error: str | None,
) -> str:
    _ = roles, fallback_capacity_fields, last_registry_error
    return (
        f"Unknown model {model_name}: using default context limits "
        f"({DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS:,} context, "
        f"{DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS:,} output). "
        f"Tune in chat with /config set {model_name} <context> <max_output>, "
        "or set ALYSIS_CONTEXT_WINDOW."
    )


def _format_strict_error(
    diagnostics: tuple[ActiveModelMetadataDiagnostic, ...],
) -> str:
    parts: list[str] = []
    for model_name, roles, fallback_fields, last_registry_error in _group_diagnostics_for_warnings(
        diagnostics
    ):
        fields_text = ", ".join(fallback_fields)
        roles_text = ", ".join(roles)
        detail = f"{model_name} (roles: {roles_text}; fallback fields: {fields_text})"
        if last_registry_error:
            detail += f"; registry detail: {last_registry_error}"
        parts.append(detail)
    joined = "; ".join(parts)
    return (
        "model_metadata_policy=strict rejected active model metadata because fallback "
        f"capacity values are in use: {joined}. {_format_fix_hint()}"
    )


def evaluate_active_model_metadata_policy(
    *,
    cfg: AppConfig | None,
    registry: ModelRegistry,
    active_models: list[ActiveModelRef],
) -> ActiveModelMetadataPolicyResult:
    policy = resolve_model_metadata_policy(cfg)
    diagnostics = collect_active_model_metadata_diagnostics(
        registry=registry,
        active_models=active_models,
    )
    if policy == "strict":
        if any(diagnostic.fallback_capacity_active for diagnostic in diagnostics):
            raise ModelMetadataPolicyError(_format_strict_error(diagnostics))
        return ActiveModelMetadataPolicyResult(
            policy=policy,
            diagnostics=diagnostics,
            warning_messages=(),
        )

    warning_messages = tuple(
        _format_warning_message(
            model_name=model_name,
            roles=roles,
            fallback_capacity_fields=fallback_capacity_fields,
            last_registry_error=last_registry_error,
        )
        for model_name, roles, fallback_capacity_fields, last_registry_error in (
            _group_diagnostics_for_warnings(diagnostics)
        )
    )
    return ActiveModelMetadataPolicyResult(
        policy=policy,
        diagnostics=diagnostics,
        warning_messages=warning_messages,
    )
