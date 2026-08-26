from __future__ import annotations

import os
import re
import traceback
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import typer
from click import Abort
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import (
    AppConfig,
    ConfigError,
    config_path,
    credentials_path,
    default_sessions_dir,
    load_config,
    save_config,
    save_persisted_profile_key,
    set_config_value,
)
from ..llm.factory import make_llm_client
from ..llm.types import LLMError
from ..profile_presets import (
    NATIVE_PROFILE_PROTOCOLS,
    PROFILE_PRESETS,
    ProfilePreset,
    advanced_provider_selection_presets,
    canonical_model_alias_for_preset,
    make_profile_from_preset,
    model_options_for_preset,
    preset_protocol_summary,
    preset_selection_label,
    provider_selection_presets,
)
from ..profiles import (
    SUBSCRIPTION_SELECTION_REQUIRED_KEY,
    ProfileSpec,
    active_subscription_selection_ready,
    add_profile,
    get_profile,
    set_active_profile,
    subscription_selection_supported,
    update_profile,
)
from ..provider_diagnostics import provider_diagnostic_warning_lines
from ..provider_model_catalog import (
    ProviderModelCatalogError,
    ProviderModelOption,
    discover_provider_models,
)
from ..reasoning_contracts import (
    UNKNOWN_CONTRACT,
    reasoning_contract_for,
    reasoning_labels_allowed_by_contract,
)
from ..sandbox_doctor import (
    BubblewrapInstallPlan,
    SandboxDiagnostic,
    SandboxPullResult,
    detect_bubblewrap_install_plan,
    diagnose_sandbox,
    format_sandbox_problem_message,
    install_bubblewrap,
    pull_sandbox_images,
)
from ..sandbox_settings import apply_sandbox_mode_to_config
from ..surface.console import make_console
from ..workspace_binding import WorkspaceBindingError, resolve_workspace_binding

_DEFAULT_WORKSPACE_KEY = "default_workspace_path"
# Explicit "the user has completed first-run setup" marker. Persisted here (and
# read by the first-run gate in commands/startup.py) so that whether to route a
# launch into setup no longer relies on the unreliable "a model happens to be
# configured" heuristic.
_ONBOARDED_KEY = "onboarded"
_CUSTOM_MODEL_VALUE = "__custom_model__"
_CUSTOM_PROVIDER_KEY = "custom"
_NVIDIA_PRESET_KEY = "nvidia"
_MODEL_REASONING_SETUP_PRESET_KEYS = frozenset({_NVIDIA_PRESET_KEY, "zai-coding-plan"})
_SETUP_REASONING_LABELS: tuple[str, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "auto",
)
_MAX_API_KEY_VALIDATION_ATTEMPTS = 3
_FALLBACK_VALIDATION_MODEL = "gpt-5.4-mini"
_VALIDATION_TIMEOUT_S = 8.0
_GEMINI_VALIDATION_TIMEOUT_S = 20.0
_GEMINI_VALIDATION_REASONING_EFFORT = "low"
_ADVANCED_PROVIDER_PRESETS_VALUE = "__advanced_provider_presets__"
_NATIVE_EXECUTION_VALUE = "__native_execution__"
_SUBSCRIPTION_EXECUTION_VALUE = "__subscription_execution__"
_RUNTIME_EXECUTION_PREFIX = "runtime:"

_ValidationStatus = Literal[
    "validated",
    "failed",
    "inconclusive",
    "skipped",
    "model_not_found",
]


class _SetupCancelled(Exception):
    pass


class _GoBack(Exception):
    """Raised by a wizard step when the user pressed Esc to go back."""


@dataclass(frozen=True)
class _ExecutionStepResult:
    backend: Literal["native", "delegated"]
    runtime: str | None = None
    label: str = "API key"
    description: str = ""
    auth_hint: str = ""


@dataclass(frozen=True)
class _DelegatedAuthResult:
    connected: bool
    summary: str


@dataclass(frozen=True)
class _ProfileStepResult:
    profile: ProfileSpec
    label: str
    preset: ProfilePreset | None


@dataclass(frozen=True)
class _ApiKeyStepResult:
    api_key: str
    validation_status: _ValidationStatus
    validation_message: str = ""


@dataclass(frozen=True)
class _ModelStepResult:
    model: str
    custom: bool = False
    reasoning_label: str = "auto"


@dataclass(frozen=True)
class _WorkspaceStepResult:
    workspace: str


@dataclass(frozen=True)
class _SandboxStepResult:
    ready: bool
    status: str


@dataclass(frozen=True)
class _ApiKeyValidationResult:
    status: _ValidationStatus
    message: str = ""


@dataclass(frozen=True)
class _ResolvedValidationModel:
    model: str
    used_fallback: bool


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    existed: bool
    data: bytes | None


def run_setup_wizard() -> bool:
    """Run the first-time setup wizard."""
    console = _resolve_console()
    escape_unavailable_reason = _escape_capture_unavailable_reason()
    if escape_unavailable_reason is not None:
        console.print(
            f"[dim]Note: this terminal does not support Esc/back navigation ({escape_unavailable_reason}). "
            "Use Ctrl+C to cancel.[/dim]"
        )

    execution_result: _ExecutionStepResult | None = None
    profile_result: _ProfileStepResult | None = None
    api_key_result: _ApiKeyStepResult | None = None
    model_result: _ModelStepResult | None = None
    workspace_result: _WorkspaceStepResult | None = None
    step_idx = 0

    try:
        while True:
            steps = _setup_step_names(execution_result)
            if step_idx >= len(steps):
                break
            step_name = steps[step_idx]
            try:
                if step_name == "welcome":
                    _print_welcome(console)
                    _wait_to_begin()
                elif step_name == "execution":
                    previous_execution = execution_result
                    execution_result = _prompt_execution(
                        console,
                        previous=previous_execution,
                    )
                    if previous_execution is not None and execution_result != previous_execution:
                        profile_result = None
                        api_key_result = None
                        model_result = None
                elif step_name == "profile":
                    previous_profile = profile_result
                    new_profile = _prompt_profile(console, previous=previous_profile)
                    if previous_profile is not None and new_profile != previous_profile:
                        api_key_result = None
                        model_result = None
                    profile_result = new_profile
                elif step_name == "api_key":
                    if profile_result is None:
                        raise ConfigError("Provider profile step was skipped.")
                    api_key_result = _prompt_api_key(
                        console,
                        profile_result,
                        previous=api_key_result,
                    )
                elif step_name == "model":
                    if profile_result is None or api_key_result is None:
                        raise ConfigError("Provider profile or API key step was skipped.")
                    model_result, api_key_result = _prompt_model(
                        console,
                        profile_result,
                        api_key_result=api_key_result,
                        previous=model_result,
                    )
                elif step_name == "workspace":
                    workspace_result = _prompt_workspace(console, previous=workspace_result)
                step_idx += 1
            except _GoBack:
                if step_idx == 0:
                    if _confirm_cancel(console, prompt="Cancel setup? [y/N]"):
                        raise _SetupCancelled() from None
                    continue
                step_idx -= 1
                continue
            except (KeyboardInterrupt, Abort, EOFError):
                if _confirm_cancel(console):
                    raise _SetupCancelled() from None
                continue

        if execution_result is None or workspace_result is None:
            raise ConfigError("Setup did not collect all required inputs.")

        if execution_result.backend == "delegated":
            cfg = _commit_delegated_setup(
                execution_result=execution_result,
                workspace_result=workspace_result,
                console=console,
            )
            auth_result = _maybe_connect_delegated_runtime(
                console=console,
                cfg=cfg,
                execution_result=execution_result,
            )
            if cfg.execution.backend == "native":
                _print_provider_diagnostic_warnings(console, cfg)
                _prompt_and_check_sandbox(console, cfg)
            _print_delegated_setup_complete(
                console=console,
                execution_result=execution_result,
                workspace_result=workspace_result,
                auth_result=auth_result,
                cfg=cfg,
            )
            return True

        if profile_result is None or api_key_result is None or model_result is None:
            raise ConfigError("Setup did not collect all required native-provider inputs.")

        cfg = _commit_setup(
            profile_result=profile_result,
            api_key_result=api_key_result,
            model_result=model_result,
            workspace_result=workspace_result,
            console=console,
        )
        _print_provider_diagnostic_warnings(console, cfg)
        sandbox_result = _prompt_and_check_sandbox(console, cfg)
        _print_setup_complete(
            console=console,
            profile_result=profile_result,
            api_key_result=api_key_result,
            model_result=model_result,
            workspace_result=workspace_result,
            sandbox_result=sandbox_result,
        )
        _maybe_offer_alysis_login(console, profile_result=profile_result, cfg=cfg)
        return True
    except _SetupCancelled:
        console.print()
        console.print("[yellow]Setup cancelled. No changes saved.[/yellow]")
        return False
    except (ConfigError, OSError) as exc:
        console.print(f"[red]Setup failed:[/red] {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - boundary translates unknown failures
        log_path = _write_exception_log("setup_wizard", exc)
        console.print(f"[red]Setup failed:[/red] {exc}")
        if log_path is not None:
            console.print(f"[dim]Details saved to: {log_path}[/dim]")
        return False


def _maybe_offer_alysis_login(
    console: Console, *, profile_result: _ProfileStepResult, cfg: AppConfig
) -> None:
    """If the user chose the hosted MiMo (login-based) preset, offer to connect now.

    That preset authenticates via `alysis login` instead of an API key.
    Running the handshake here saves a separate step; declining is harmless (the
    profile is already configured and `alysis login` can be run any time).
    """
    from ..alysis_cloud import PROFILE_KEY

    preset = profile_result.preset
    if preset is None or preset.key != PROFILE_KEY:
        return

    console.print()
    try:
        connect = _prompt_yes_no(
            "Connect your Alysis Code account now? [Y/n]",
            default=True,
        )
    except _GoBack:
        connect = False

    if not connect:
        console.print("[dim]Run `alysis login` whenever you're ready to connect.[/dim]")
        return

    from .. import account_login

    try:
        result = account_login.login(
            cfg, output_write=lambda message: console.print(message, highlight=False)
        )
    except (account_login.AlysisLoginError, ConfigError) as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        console.print("[dim]You can finish this later with `alysis login`.[/dim]")
        return

    who = f" as [bold]{result.email}[/bold]" if result.email else ""
    console.print(f"[green]Logged in{who}.[/green] Your Alysis Code account is connected.")
    console.print(
        f"[dim]Default model: [/dim][bold]{result.model}[/bold][dim] · "
        "switch with /model in chat or `alysis config`.[/dim]"
    )


def _resolve_console() -> Console:
    return make_console()


def _print_welcome(console: Console) -> None:
    body = Group(
        Text("Alysis Code is a coding agent that runs in your terminal."),
        Text("First choose how you want to connect Alysis Code to AI models."),
        Text(""),
        Text(" API key: connect directly to a supported model provider."),
        Text(" AI subscription: sign in through a supported provider's official client."),
        Text(""),
        Text("Both paths ask for the workspace folder you want to work on."),
        Text(""),
        Text("[Enter] begin  [Esc] cancel", style="dim"),
    )
    console.print()
    console.print(Panel(body, title="Welcome to Alysis Code", border_style="cyan"))


def _wait_to_begin() -> None:
    value = _esc_aware_text_input("Press Enter to begin", default="", show_default=False)
    if _is_cancel_token(value):
        raise _GoBack()


def _runtime_setup_options() -> tuple[Any, ...]:
    from ..provider_auth import provider_auth_setup_options

    return tuple(provider_auth_setup_options())


def _connection_method_picker_rows() -> list[tuple[str, str, str]]:
    return [
        (
            _NATIVE_EXECUTION_VALUE,
            "Use an API key",
            "Connect directly to a supported model provider.",
        ),
        (
            _SUBSCRIPTION_EXECUTION_VALUE,
            "Use an AI subscription",
            "Sign in through a supported provider's official client.",
        ),
    ]


def _subscription_picker_rows() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for option in _runtime_setup_options():
        runtime_id = str(getattr(option, "id", "") or "").strip()
        label = str(getattr(option, "label", "") or runtime_id).strip()
        description = str(getattr(option, "description", "") or "").strip()
        if not runtime_id or not label:
            continue
        rows.append((f"{_RUNTIME_EXECUTION_PREFIX}{runtime_id}", label, description))
    return rows


def _connect_provider_picker_rows() -> list[tuple[str, str, str]]:
    """One merged "Connect a provider" list.

    Subscription sign-ins and API-key providers appear side by side, so the
    auth method is a property of the row rather than an upfront branch.
    Subscription rows lead (including the Alysis Code account itself), then the
    hosted providers in preset order; the advanced sub-list (local endpoints,
    custom URLs, and legacy presets) stays behind the final row. Values are
    either a preset key, a ``runtime:<id>`` subscription value, or
    :data:`_ADVANCED_PROVIDER_PRESETS_VALUE`.
    """
    rows: list[tuple[str, str, str]] = []
    # The Alysis Code account leads: browser sign-in, no API key, free daily
    # hosted DeepSeek included — the zero-friction way in.
    rows.append(
        (
            "alysis",
            "Alysis Code account",
            "Free daily DeepSeek included — browser sign-in, no API key.",
        )
    )
    for value, label, description in _subscription_picker_rows():
        rows.append((value, label, description or "Browser sign-in with your subscription."))
    rows.extend(
        (preset.key, preset_selection_label(preset), "API key") for preset in _setup_presets()
    )
    rows.append(
        (
            _ADVANCED_PROVIDER_PRESETS_VALUE,
            "Advanced / local / compatibility providers",
            "Local endpoints (Ollama, LM Studio, vLLM), custom URLs, and legacy "
            "OpenAI-compatible presets.",
        )
    )
    return rows


def _runtime_setup_option(runtime_id: str) -> Any:
    normalized = str(runtime_id or "").strip()
    for option in _runtime_setup_options():
        if str(getattr(option, "id", "") or "").strip() == normalized:
            return option
    raise ConfigError(f"Unknown AI subscription connection: {normalized or '(empty)'}")


def _execution_result_from_value(value: str) -> _ExecutionStepResult:
    selected = str(value or "").strip()
    if selected == _NATIVE_EXECUTION_VALUE:
        return _ExecutionStepResult(backend="native")
    if not selected.startswith(_RUNTIME_EXECUTION_PREFIX):
        raise ConfigError(f"Unknown connection method: {selected or '(empty)'}")
    runtime_id = selected.removeprefix(_RUNTIME_EXECUTION_PREFIX).strip()
    option = _runtime_setup_option(runtime_id)
    return _ExecutionStepResult(
        backend="delegated",
        runtime=runtime_id,
        label=str(getattr(option, "label", "") or runtime_id).strip(),
        description=str(getattr(option, "description", "") or "").strip(),
        auth_hint=str(getattr(option, "auth_hint", "") or "").strip(),
    )


def _prompt_execution(
    console: Console,
    *,
    previous: _ExecutionStepResult | None = None,
) -> _ExecutionStepResult:
    current_method = (
        _SUBSCRIPTION_EXECUTION_VALUE
        if previous is not None and previous.backend == "delegated"
        else _NATIVE_EXECUTION_VALUE
    )
    while True:
        selected_method = _run_wizard_picker(
            console=console,
            title="Connection Method",
            subtitle="How would you like to connect Alysis Code to AI models?",
            rows=_connection_method_picker_rows(),
            current_value=current_method,
            cancel_hint="back",
            invalid_hint="Pick a connection method or press Esc to go back.",
        )
        if selected_method is None:
            raise _GoBack()
        if selected_method == _NATIVE_EXECUTION_VALUE:
            result = _execution_result_from_value(selected_method)
            console.print("[green]Connection method selected:[/green] Use an API key")
            return result
        if selected_method != _SUBSCRIPTION_EXECUTION_VALUE:
            raise ConfigError(f"Unknown connection method: {selected_method or '(empty)'}")

        rows = _subscription_picker_rows()
        if not rows:
            raise ConfigError("No AI subscription connections are available.")
        current_runtime = rows[0][0]
        if previous is not None and previous.backend == "delegated" and previous.runtime:
            previous_runtime = f"{_RUNTIME_EXECUTION_PREFIX}{previous.runtime}"
            if any(value == previous_runtime for value, _label, _description in rows):
                current_runtime = previous_runtime
        selected_runtime = _run_wizard_picker(
            console=console,
            title="AI Subscription",
            subtitle="Choose the subscription you want to connect.",
            rows=rows,
            current_value=current_runtime,
            cancel_hint="connection methods",
            invalid_hint="Pick an AI subscription or press Esc to go back.",
        )
        if selected_runtime is None:
            current_method = _SUBSCRIPTION_EXECUTION_VALUE
            continue
        result = _execution_result_from_value(selected_runtime)
        console.print("[green]Connection method selected:[/green] Use an AI subscription")
        console.print(f"[green]Subscription selected:[/green] {result.label}")
        return result


def _setup_step_names(
    execution_result: _ExecutionStepResult | None,
) -> tuple[str, ...]:
    if execution_result is None:
        return ("welcome", "execution")
    if execution_result.backend == "delegated":
        return ("welcome", "execution", "workspace")
    return (
        "welcome",
        "execution",
        "profile",
        "api_key",
        "model",
        "workspace",
    )


def _prompt_profile(
    console: Console,
    *,
    previous: _ProfileStepResult | None = None,
) -> _ProfileStepResult:
    rows = _provider_picker_rows(_setup_presets())
    current_value = rows[0][0]
    if previous is not None:
        current_value = previous.preset.key if previous.preset is not None else _CUSTOM_PROVIDER_KEY
    selected = _run_wizard_picker(
        console=console,
        title="Provider Profile",
        subtitle="Pick the provider you want Alysis Code to use.",
        rows=rows,
        current_value=current_value,
        cancel_hint="back",
        invalid_hint="Pick a provider first; you'll enter the key in the next step.",
    )
    if selected == _ADVANCED_PROVIDER_PRESETS_VALUE:
        advanced_rows = [
            (preset.key, preset_selection_label(preset), _preset_description(preset))
            for preset in _advanced_setup_presets()
        ]
        selected = _run_wizard_picker(
            console=console,
            title="Advanced Provider Profile",
            subtitle="Pick a local, custom, or legacy OpenAI-compatible provider preset.",
            rows=advanced_rows,
            current_value=advanced_rows[0][0] if advanced_rows else "openai",
            cancel_hint="back",
            invalid_hint="Pick a provider preset or press Esc to go back.",
        )
    if selected is None:
        raise _GoBack()

    preset = _preset_by_key(selected)
    if preset is None:
        raise ConfigError(f"Unknown provider preset: {selected}")
    if preset.key == _CUSTOM_PROVIDER_KEY:
        profile = _prompt_custom_profile(
            previous.profile if previous is not None and previous.preset is None else None
        )
        return _ProfileStepResult(profile=profile, label=profile.name, preset=None)

    profile = make_profile_from_preset(preset)
    console.print(f"[green]Provider selected:[/green] {preset.label}")
    _print_preset_warning(console, preset)
    return _ProfileStepResult(profile=profile, label=preset.label, preset=preset)


def _prompt_custom_profile(previous: ProfileSpec | None = None) -> ProfileSpec:
    while True:
        name = (
            _esc_aware_text_input(
                "Profile name",
                default=(previous.name if previous is not None else "custom"),
                show_default=True,
            )
            .strip()
            .lower()
        )
        if _is_cancel_token(name):
            raise _GoBack()
        if name:
            break
        _resolve_console().print("[red]Profile name is required.[/red]")
    while True:
        base_url = _esc_aware_text_input(
            "Base URL",
            default=(previous.base_url if previous is not None else ""),
            show_default=previous is not None,
        ).strip()
        if _is_cancel_token(base_url):
            raise _GoBack()
        if base_url:
            break
        _resolve_console().print("[red]Base URL is required.[/red]")
    headers = _prompt_extra_headers(previous.extra_headers if previous is not None else None)
    return ProfileSpec(
        name=name,
        protocol=previous.protocol if previous is not None else "openai_compat",
        base_url=base_url,
        api_key_env=previous.api_key_env if previous is not None else None,
        auth_provider=previous.auth_provider if previous is not None else None,
        extra_headers=headers,
        default_model=previous.default_model if previous is not None else "",
        reasoning_effort=previous.reasoning_effort if previous is not None else None,
        reasoning_trace_adapter=(
            previous.reasoning_trace_adapter if previous is not None else "auto"
        ),
        web_search_adapter=previous.web_search_adapter if previous is not None else "auto",
        web_search_model=previous.web_search_model if previous is not None else "",
        notes=previous.notes if previous is not None else "Custom OpenAI-compatible endpoint.",
        cache_capability=previous.cache_capability if previous is not None else None,
    )


def _prompt_extra_headers(previous: dict[str, str] | None = None) -> dict[str, str]:
    previous_text = ", ".join(f"{key}={value}" for key, value in (previous or {}).items())
    while True:
        headers_raw = _esc_aware_text_input(
            "Extra headers (k=v, comma-separated)",
            default=previous_text,
            show_default=bool(previous_text),
        ).strip()
        if _is_cancel_token(headers_raw):
            raise _GoBack()
        headers: dict[str, str] = {}
        valid = True
        for item in headers_raw.split(","):
            text = item.strip()
            if not text:
                continue
            if "=" not in text:
                _resolve_console().print("[red]Extra headers must use k=v syntax.[/red]")
                valid = False
                break
            key, value = text.split("=", 1)
            if key.strip() and value.strip():
                headers[key.strip()] = value.strip()
        if valid:
            return headers


def _prompt_api_key(
    console: Console,
    profile_result: _ProfileStepResult,
    *,
    previous: _ApiKeyStepResult | None = None,
) -> _ApiKeyStepResult:
    profile = profile_result.profile
    console.print()
    console.print(Panel(_api_key_panel_text(profile_result), title="API Key", border_style="cyan"))
    required = bool(profile.api_key_env)
    attempts = 0
    last_key = previous.api_key if previous is not None else ""
    while attempts < _MAX_API_KEY_VALIDATION_ATTEMPTS:
        prompt_label = "Paste your API key" if required else "API key (optional)"
        if previous is not None:
            prompt_label += " (Enter to keep current)"
        value = _esc_aware_text_input(
            prompt_label,
            default="",
            hide_input=True,
            show_default=False,
        ).strip()
        if _is_cancel_token(value):
            raise _GoBack()
        if not value and previous is not None:
            return previous
        if not value:
            if required:
                console.print("[red]API key is required to continue.[/red]")
                attempts += 1
                continue
            console.print("[yellow]No API key stored for this profile.[/yellow]")
            return _ApiKeyStepResult(
                api_key="",
                validation_status="skipped",
                validation_message="No API key provided.",
            )

        last_key = value
        console.print("[dim]Validating...[/dim]")
        validation = _validate_api_key(
            profile=profile,
            api_key=value,
            suggested_models=_suggested_models(profile_result),
            validation_model=_validation_model_hint(profile_result),
        )
        if validation.status == "validated":
            console.print("[green]Key validated.[/green]")
            if validation.message:
                console.print(f"[yellow]{validation.message}[/yellow]")
            return _ApiKeyStepResult(
                api_key=value,
                validation_status="validated",
                validation_message=validation.message,
            )
        if validation.status == "inconclusive":
            console.print(f"[yellow]{validation.message} Continuing without validation.[/yellow]")
            return _ApiKeyStepResult(
                api_key=value,
                validation_status="inconclusive",
                validation_message=validation.message,
            )
        if validation.status == "model_not_found":
            console.print(
                f"[yellow]{validation.message} We'll verify your chosen model next.[/yellow]"
            )
            return _ApiKeyStepResult(
                api_key=value,
                validation_status="model_not_found",
                validation_message=validation.message,
            )

        attempts += 1
        console.print(
            f"[yellow]Key validation failed:[/yellow] {validation.message or 'provider rejected the key'}"
        )
        if attempts >= _MAX_API_KEY_VALIDATION_ATTEMPTS:
            console.print(
                "[yellow]Continuing with the last key. You can fix it later in /config.[/yellow]"
            )
            return _ApiKeyStepResult(
                api_key=last_key,
                validation_status="failed",
                validation_message=validation.message,
            )
        if not _prompt_yes_no("Re-enter API key now? [Y/n]", default=True):
            console.print(
                "[yellow]Continuing without validation. You can fix it later in /config.[/yellow]"
            )
            return _ApiKeyStepResult(
                api_key=value,
                validation_status="failed",
                validation_message=validation.message,
            )

    return _ApiKeyStepResult(
        api_key=last_key,
        validation_status="failed",
        validation_message="Provider rejected the API key.",
    )


def _api_key_panel_text(profile_result: _ProfileStepResult) -> str:
    profile = profile_result.profile
    lines = [f"Alysis Code will use {profile_result.label}."]
    lines.append(
        "Protocol: "
        + (
            f"native first-party ({profile.protocol})"
            if profile.protocol in NATIVE_PROFILE_PROTOCOLS
            else "OpenAI-compatible"
        )
    )
    if profile.base_url:
        lines.append(f"Provider URL: {profile.base_url}")
    if profile.api_key_env:
        lines.append(f"The key can also be set via env: {profile.api_key_env}")
    else:
        lines.append("This profile does not declare a required API key env variable.")
    lines.append("")
    lines.append("Enter to confirm. Esc to go back. Ctrl+C to cancel.")
    return "\n".join(lines)


def _suggested_models(profile_result: _ProfileStepResult) -> tuple[str, ...]:
    if profile_result.preset is None:
        return ()
    return tuple(
        str(model).strip() for model in profile_result.preset.suggested_models if str(model).strip()
    )


def _validation_model_hint(profile_result: _ProfileStepResult) -> str:
    preset = profile_result.preset
    if preset is None:
        return ""
    model = str(preset.validation_model or "").strip()
    if not model:
        return ""
    return canonical_model_alias_for_preset(preset, model)


def _validate_api_key(
    *,
    profile: ProfileSpec,
    api_key: str,
    model: str | None = None,
    suggested_models: tuple[str, ...] = (),
    validation_model: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> _ApiKeyValidationResult:
    if not api_key.strip():
        return _ApiKeyValidationResult(status="skipped", message="No API key provided.")
    base_url = str(profile.base_url or "").strip().rstrip("/")
    if not base_url:
        return _ApiKeyValidationResult(
            status="inconclusive", message="Could not reach provider: Base URL is missing."
        )

    resolved_validation_model = _resolve_validation_model(
        profile=profile,
        model=model,
        suggested_models=suggested_models,
        validation_model=validation_model,
    )
    validation_profile = ProfileSpec(
        name=profile.name,
        protocol=profile.protocol,
        base_url=base_url,
        api_key_env=profile.api_key_env,
        extra_headers=dict(profile.extra_headers),
        default_model=resolved_validation_model.model,
        reasoning_effort=profile.reasoning_effort,
        reasoning_trace_adapter=profile.reasoning_trace_adapter,
        web_search_adapter=profile.web_search_adapter,
        web_search_model=profile.web_search_model,
        notes=profile.notes,
    )
    cfg = AppConfig(
        model=resolved_validation_model.model,
        provider_retry_max_retries=0,
    )
    client = make_llm_client(
        cfg=cfg,
        api_key=api_key,
        model=resolved_validation_model.model,
        timeout_s=_validation_timeout_s(validation_profile),
        temperature=0.0,
        reasoning_effort=_validation_reasoning_effort(validation_profile),
        transport=transport,
        profile=validation_profile,
    )
    try:
        client.chat(
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.0,
        )
    except LLMError as exc:
        return _validation_result_from_error(
            exc,
            base_url=base_url,
            model=resolved_validation_model.model,
        )

    message = ""
    if resolved_validation_model.used_fallback:
        message = (
            f"Validated API key using '{resolved_validation_model.model}'. "
            "The selected model will be verified next."
        )
    return _ApiKeyValidationResult(status="validated", message=message)


def _resolve_validation_model(
    *,
    profile: ProfileSpec,
    model: str | None,
    suggested_models: tuple[str, ...],
    validation_model: str | None = None,
) -> _ResolvedValidationModel:
    explicit_model = str(model or "").strip()
    if explicit_model:
        return _ResolvedValidationModel(model=explicit_model, used_fallback=False)
    validation_candidate = str(validation_model or "").strip()
    if validation_candidate:
        return _ResolvedValidationModel(model=validation_candidate, used_fallback=True)
    for candidate in (profile.default_model, *suggested_models):
        normalized = str(candidate or "").strip()
        if normalized:
            return _ResolvedValidationModel(model=normalized, used_fallback=False)
    return _ResolvedValidationModel(model=_FALLBACK_VALIDATION_MODEL, used_fallback=True)


def _validation_timeout_s(profile: ProfileSpec) -> float:
    if _is_gemini_profile(profile):
        return _GEMINI_VALIDATION_TIMEOUT_S
    return _VALIDATION_TIMEOUT_S


def _validation_reasoning_effort(profile: ProfileSpec) -> str | None:
    if _is_gemini_profile(profile):
        return _GEMINI_VALIDATION_REASONING_EFFORT
    if _is_zai_coding_plan_profile(profile):
        # The plan defaults to max. A low-effort one-token validation proves
        # model access while consuming the smallest documented reasoning tier.
        return "low"
    return None


def _is_gemini_profile(profile: ProfileSpec) -> bool:
    name = str(profile.name or "").strip().casefold()
    base_url = str(profile.base_url or "").strip().casefold()
    return name == "gemini" or "generativelanguage.googleapis.com" in base_url


def _is_zai_coding_plan_profile(profile: ProfileSpec) -> bool:
    name = str(profile.name or "").strip().casefold()
    base_url = str(profile.base_url or "").strip().rstrip("/").casefold()
    return name == "zai-coding-plan" or base_url == "https://api.z.ai/api/coding/paas/v4"


def _validation_result_from_error(
    exc: LLMError,
    *,
    base_url: str,
    model: str,
) -> _ApiKeyValidationResult:
    status_code = _http_status_from_llm_error(exc)
    error_text = str(exc)
    error_text_lower = error_text.casefold()

    if status_code == 200:
        return _ApiKeyValidationResult(status="validated")
    if status_code in {401, 403}:
        return _ApiKeyValidationResult(
            status="failed",
            message=f"Provider rejected the API key (HTTP {status_code}).",
        )
    if status_code == 400 and _is_model_not_found_error(error_text_lower):
        return _ApiKeyValidationResult(
            status="model_not_found",
            message=f"Model '{model}' not found at this provider. Pick a different model in the next step.",
        )
    if status_code == 404:
        return _ApiKeyValidationResult(
            status="inconclusive",
            message=f"Provider endpoint not found at {base_url}. Check the base URL.",
        )
    if status_code == 429:
        return _ApiKeyValidationResult(
            status="inconclusive",
            message="Provider rate-limited the validation request. Continuing without validation.",
        )
    if status_code is not None and status_code >= 500:
        return _ApiKeyValidationResult(
            status="inconclusive",
            message=f"Provider returned HTTP {status_code} during validation.",
        )

    cause = _root_cause(exc)
    if isinstance(cause, httpx.TimeoutException):
        return _ApiKeyValidationResult(
            status="inconclusive",
            message=f"Could not reach {base_url}: validation request timed out.",
        )
    if isinstance(cause, httpx.HTTPError):
        return _ApiKeyValidationResult(
            status="inconclusive",
            message=f"Could not reach {base_url}: {cause}",
        )
    return _ApiKeyValidationResult(
        status="inconclusive",
        message=f"Could not verify {base_url}: {exc}",
    )


def _is_model_not_found_error(error_text_lower: str) -> bool:
    if "not_found" in error_text_lower and "model" in error_text_lower:
        return True
    if "model_not_found" in error_text_lower:
        return True
    if "model not found" in error_text_lower:
        return True
    if re.search(
        r"\bmodel\b.{0,80}\b(?:does not exist|not exist|not found|unknown)", error_text_lower
    ):
        return True
    return (
        re.search(
            r"\b(?:does not exist|not exist|not found|unknown)\b.{0,80}\bmodel\b", error_text_lower
        )
        is not None
    )


def _http_status_from_llm_error(exc: LLMError) -> int | None:
    match = re.search(r"\bLLM error\s+(\d{3})\b", str(exc))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _root_cause(exc: BaseException) -> BaseException:
    cause = exc
    seen: set[int] = set()
    while cause.__cause__ is not None and id(cause.__cause__) not in seen:
        seen.add(id(cause))
        cause = cause.__cause__
    return cause


def _prompt_model(
    console: Console,
    profile_result: _ProfileStepResult,
    *,
    api_key_result: _ApiKeyStepResult,
    previous: _ModelStepResult | None = None,
) -> tuple[_ModelStepResult, _ApiKeyStepResult]:
    current_previous = previous
    discovered_models, catalog_warning = _discover_setup_provider_models(
        profile_result,
        api_key=api_key_result.api_key,
    )
    if catalog_warning:
        console.print(f"[yellow]{catalog_warning}[/yellow]")
    while True:
        model_result = _prompt_model_choice(
            console,
            profile_result,
            previous=current_previous,
            discovered_models=discovered_models,
        )
        updated_api_key_result = _validate_selected_model(
            console=console,
            profile_result=profile_result,
            api_key_result=api_key_result,
            model_result=model_result,
        )
        if updated_api_key_result.validation_status == "model_not_found":
            current_previous = model_result
            if model_result.custom and _prompt_yes_no(
                f"Use custom model '{model_result.model}' anyway? [y/N]",
                default=False,
            ):
                warning = (
                    f"Model '{model_result.model}' was not confirmed; "
                    "provider reported it was missing and you chose to use it anyway."
                )
                return model_result, replace(
                    updated_api_key_result,
                    validation_status="inconclusive",
                    validation_message=warning,
                )
            continue
        reasoning_label = _prompt_setup_reasoning_label(
            console,
            profile_result=profile_result,
            model=model_result.model,
            current=model_result.reasoning_label,
        )
        return replace(model_result, reasoning_label=reasoning_label), updated_api_key_result


def _prompt_model_choice(
    console: Console,
    profile_result: _ProfileStepResult,
    *,
    previous: _ModelStepResult | None = None,
    discovered_models: tuple[ProviderModelOption, ...] = (),
) -> _ModelStepResult:
    rows = _model_picker_rows(profile_result, discovered_models=discovered_models)
    row_values = {value for value, _label, _description in rows}
    current_value = rows[0][0]
    if previous is not None:
        current_value = previous.model if previous.model in row_values else _CUSTOM_MODEL_VALUE
    selected = _run_wizard_picker(
        console=console,
        title="Default Model",
        subtitle=f"Pick the model Alysis Code will use by default for {profile_result.label}.",
        rows=rows,
        current_value=current_value,
        cancel_hint="back",
    )
    if selected is None:
        raise _GoBack()
    if selected == _CUSTOM_MODEL_VALUE:
        default_model = (
            previous.model if previous is not None and previous.model not in row_values else ""
        )
        model = _prompt_custom_model_name(previous=default_model or None)
        is_custom = True
    else:
        model = selected
        is_custom = False
    if profile_result.preset is not None:
        model = canonical_model_alias_for_preset(profile_result.preset, model)
    if not model.strip():
        raise ConfigError("Default model is required.")
    console.print(f"[green]Default model:[/green] {model}")
    return _ModelStepResult(model=model.strip(), custom=is_custom)


def _validate_selected_model(
    *,
    console: Console,
    profile_result: _ProfileStepResult,
    api_key_result: _ApiKeyStepResult,
    model_result: _ModelStepResult,
) -> _ApiKeyStepResult:
    if not api_key_result.api_key:
        return api_key_result
    if api_key_result.validation_status == "failed":
        console.print(
            "[yellow]Skipping model validation because the provider already rejected the API key.[/yellow]"
        )
        return api_key_result

    console.print("[dim]Validating selected model...[/dim]")
    validation = _validate_api_key(
        profile=profile_result.profile,
        api_key=api_key_result.api_key,
        model=model_result.model,
        suggested_models=_suggested_models(profile_result),
    )
    if validation.status == "validated":
        console.print("[green]Model validated.[/green]")
    elif validation.status == "model_not_found":
        console.print(f"[red]{validation.message}[/red]")
    elif validation.status == "failed":
        console.print(f"[red]{validation.message}[/red]")
    elif validation.status == "inconclusive":
        console.print(f"[yellow]{validation.message}[/yellow]")
    return replace(
        api_key_result,
        validation_status=validation.status,
        validation_message=validation.message,
    )


def _discover_setup_provider_models(
    profile_result: _ProfileStepResult,
    *,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
) -> tuple[tuple[ProviderModelOption, ...], str]:
    preset = profile_result.preset
    if preset is None or preset.key != _NVIDIA_PRESET_KEY:
        return (), ""
    try:
        options = discover_provider_models(
            profile=profile_result.profile,
            api_key=api_key,
            transport=transport,
        )
    except ProviderModelCatalogError:
        return (), (
            "NVIDIA's live model catalog is unavailable. Recommended models and "
            "custom model entry are still available."
        )
    return tuple(sorted(options, key=lambda option: option.id.casefold())), ""


def _model_picker_rows(
    profile_result: _ProfileStepResult,
    *,
    discovered_models: tuple[ProviderModelOption, ...] = (),
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    preset = profile_result.preset
    if preset is not None:
        for value, label, description in model_options_for_preset(preset):
            if value in seen:
                continue
            seen.add(value)
            rows.append((value, label, description))
    if profile_result.profile.default_model:
        normalized = str(profile_result.profile.default_model).strip()
        if preset is not None:
            normalized = canonical_model_alias_for_preset(preset, normalized)
        if normalized and normalized not in seen:
            seen.add(normalized)
            rows.append((normalized, normalized, "current profile default"))
    custom_row = (
        _CUSTOM_MODEL_VALUE,
        "Type a custom model name",
        "Use any model supported by this provider",
    )
    if preset is not None and preset.key == _NVIDIA_PRESET_KEY:
        # Keep manual entry reachable without scrolling through NVIDIA's large
        # live catalog. Curated recommendations remain above it; live inventory
        # follows below.
        rows.append(custom_row)
    for option in discovered_models:
        model_id = str(option.id or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        description = str(option.description or "").strip()
        if not description:
            owner = model_id.partition("/")[0]
            description = (
                f"live NVIDIA catalog · hosted model from {owner}"
                if owner and owner != model_id
                else "live NVIDIA catalog"
            )
        label = option.label or model_id
        if option.chat_compatibility == "unknown":
            label = f"{label} (chat compatibility unverified)"
            description = (
                f"{description} · provider catalog does not declare endpoint compatibility; "
                "selection is validated before setup completes"
            )
        rows.append((model_id, label, description))
    if custom_row not in rows:
        rows.append(custom_row)
    return rows


def _setup_reasoning_labels(
    profile_result: _ProfileStepResult,
    *,
    model: str,
    current: str = "auto",
) -> tuple[str, ...]:
    preset = profile_result.preset
    if preset is None or preset.key not in _MODEL_REASONING_SETUP_PRESET_KEYS:
        return ("auto",)
    contract = reasoning_contract_for(
        preset.provider_key,
        model,
        preset_key=preset.key,
    )
    if contract is UNKNOWN_CONTRACT:
        return ("auto",)
    labels = reasoning_labels_allowed_by_contract(
        contract,
        _SETUP_REASONING_LABELS,
        current=current if current in _SETUP_REASONING_LABELS else "auto",
    )
    return tuple(labels) or ("auto",)


def _prompt_setup_reasoning_label(
    console: Console,
    *,
    profile_result: _ProfileStepResult,
    model: str,
    current: str = "auto",
) -> str:
    preset = profile_result.preset
    if preset is None or preset.key not in _MODEL_REASONING_SETUP_PRESET_KEYS:
        return current
    labels = _setup_reasoning_labels(
        profile_result,
        model=model,
        current=current,
    )
    if labels == ("auto",) and preset.key == _NVIDIA_PRESET_KEY:
        console.print(
            "[dim]Reasoning controls for this NVIDIA-hosted model are not verified; "
            "using the provider default without sending a guessed parameter.[/dim]"
        )
        return "auto"
    descriptions = {
        "off": "disable reasoning using this model's documented provider control",
        "low": "lower reasoning effort",
        "medium": "balanced reasoning effort",
        "high": "full reasoning effort",
        "max": "maximum reasoning effort",
        "auto": "use the provider's documented default",
    }
    rows = [
        (label, label, descriptions.get(label, "provider-documented reasoning effort"))
        for label in labels
    ]
    selected = _run_wizard_picker(
        console=console,
        title="Reasoning Effort",
        subtitle=f"Choose a verified reasoning mode for {model} on {preset.label}.",
        rows=rows,
        current_value=current if current in labels else "auto",
        cancel_hint="models",
    )
    if selected is None:
        raise _GoBack()
    return selected


def _prompt_custom_model_name(*, previous: str | None = None) -> str:
    while True:
        value = _esc_aware_text_input(
            "Model",
            default=previous or "",
            show_default=previous is not None,
        ).strip()
        if _is_cancel_token(value):
            raise _GoBack()
        if value:
            return value
        _resolve_console().print("[red]Model is required.[/red]")


def _prompt_workspace(
    console: Console,
    *,
    previous: _WorkspaceStepResult | None = None,
) -> _WorkspaceStepResult:
    default_workspace = (
        Path(previous.workspace).expanduser()
        if previous is not None
        else _suggest_workspace_default()
    )
    console.print()
    console.print(
        Panel(
            Group(
                Text("Alysis Code reads and edits files inside one folder."),
                Text(f"Suggestion: {_display_path(default_workspace)}", style="dim"),
                Text(""),
                Text("Enter to confirm. Esc to go back. Ctrl+C to cancel.", style="dim"),
            ),
            title="Workspace",
            border_style="cyan",
        )
    )
    while True:
        raw = _esc_aware_text_input(
            "Workspace folder",
            default=os.fspath(default_workspace),
            show_default=True,
        )
        if _is_cancel_token(raw):
            raise _GoBack()
        selected = Path(str(raw or default_workspace)).expanduser().resolve()
        try:
            binding = resolve_workspace_binding(
                selected,
                allow_broad_workspace=True,
                source="setup_wizard",
            )
        except WorkspaceBindingError as exc:
            console.print(f"[red]{exc}[/red]")
            continue

        workspace = os.fspath(binding.requested_path)
        console.print(f"[green]Workspace:[/green] {workspace}")
        return _WorkspaceStepResult(workspace=workspace)


def _commit_setup(
    *,
    profile_result: _ProfileStepResult,
    api_key_result: _ApiKeyStepResult,
    model_result: _ModelStepResult,
    workspace_result: _WorkspaceStepResult,
    console: Console,
) -> AppConfig:
    snapshots = [_snapshot_file(config_path()), _snapshot_file(credentials_path())]
    try:
        cfg = load_config()
        cfg.execution.backend = "native"
        cfg.execution.runtime = None
        add_profile(cfg, profile_result.profile)
        set_active_profile(cfg, profile_result.profile.name)
        set_config_value(cfg, "model", model_result.model)
        reasoning_label = str(model_result.reasoning_label or "auto").strip().casefold()
        reasoning_effort = (
            None
            if reasoning_label == "auto"
            else ("none" if reasoning_label == "off" else reasoning_label)
        )
        cfg.llm_reasoning_effort = reasoning_effort
        cfg.llm_enable_thinking = None if reasoning_label == "auto" else reasoning_label != "off"
        update_profile(
            cfg,
            profile_result.profile.name,
            default_model=model_result.model,
            reasoning_effort=reasoning_effort,
        )
        extra_fields = dict(cfg.extra_fields or {})
        if reasoning_label not in {"auto", "off"}:
            extra_fields["llm_thinking_label"] = reasoning_label
        else:
            extra_fields.pop("llm_thinking_label", None)
        extra_fields[_DEFAULT_WORKSPACE_KEY] = workspace_result.workspace
        extra_fields[_ONBOARDED_KEY] = True
        extra_fields.pop("subscription_reconnect_required", None)
        cfg.extra_fields = extra_fields
        save_config(cfg)
        if api_key_result.api_key:
            save_persisted_profile_key(profile_result.profile.name, api_key_result.api_key)
        console.print("[green]Setup saved.[/green]")
        return cfg
    except (ConfigError, OSError) as exc:
        rollback_errors = _rollback_partial_persistence(snapshots)
        console.print(f"[red]Failed to save setup:[/red] {exc}")
        if rollback_errors:
            for error in rollback_errors:
                console.print(f"[red]Rollback failed:[/red] {error}")
            raise ConfigError(
                f"Failed to save setup: {exc}. Rollback also failed; inspect config and credentials files manually."
            ) from exc
        raise ConfigError(f"Failed to save setup: {exc}") from exc


def _commit_delegated_setup(
    *,
    execution_result: _ExecutionStepResult,
    workspace_result: _WorkspaceStepResult,
    console: Console,
) -> AppConfig:
    runtime_id = str(execution_result.runtime or "").strip()
    if execution_result.backend != "delegated" or not runtime_id:
        raise ConfigError("AI subscription setup requires a provider connection.")

    snapshots = [_snapshot_file(config_path()), _snapshot_file(credentials_path())]
    try:
        from ..config import AgentRuntimeSettings

        option = _runtime_setup_option(runtime_id)
        cfg = load_config()
        if _is_direct_subscription(runtime_id):
            profile = _direct_subscription_profile(cfg, runtime_id)
            add_profile(cfg, profile, allow_auth_profile_update=True)
            set_active_profile(cfg, profile.name)
            cfg.execution.backend = "native"
            cfg.execution.runtime = None
            cfg.model = profile.default_model
            effective_effort = (
                None if profile.reasoning_effort in {None, "auto"} else profile.reasoning_effort
            )
            cfg.llm_reasoning_effort = effective_effort
            cfg.llm_enable_thinking = (
                None if effective_effort is None else effective_effort != "none"
            )
        else:
            cfg.execution.backend = "delegated"
            cfg.execution.runtime = runtime_id
            if runtime_id not in cfg.agent_runtimes:
                cfg.agent_runtimes[runtime_id] = AgentRuntimeSettings(
                    adapter=str(getattr(option, "adapter", "") or "").strip(),
                    executable=str(getattr(option, "default_executable", "") or "").strip(),
                )
        extra_fields = dict(cfg.extra_fields or {})
        extra_fields[_DEFAULT_WORKSPACE_KEY] = workspace_result.workspace
        if _is_direct_subscription(runtime_id):
            extra_fields[_ONBOARDED_KEY] = False
            extra_fields["subscription_reconnect_required"] = True
            if not profile.default_model or profile.reasoning_effort is None:
                extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = runtime_id
        else:
            extra_fields[_ONBOARDED_KEY] = True
            extra_fields.pop("subscription_reconnect_required", None)
        cfg.extra_fields = extra_fields
        save_config(cfg)
        console.print("[green]Setup saved.[/green]")
        return cfg
    except (ConfigError, OSError, ValueError) as exc:
        rollback_errors = _rollback_partial_persistence(snapshots)
        console.print(f"[red]Failed to save setup:[/red] {exc}")
        if rollback_errors:
            for error in rollback_errors:
                console.print(f"[red]Rollback failed:[/red] {error}")
            raise ConfigError(
                f"Failed to save setup: {exc}. Rollback also failed; inspect config and credentials files manually."
            ) from exc
        raise ConfigError(f"Failed to save setup: {exc}") from exc


def _delegated_runtime_id(execution_result: _ExecutionStepResult) -> str:
    runtime_id = str(execution_result.runtime or "").strip()
    if execution_result.backend != "delegated" or not runtime_id:
        raise ConfigError("AI subscription setup requires a provider connection.")
    return runtime_id


def _delegated_auth_result(account: Any) -> _DelegatedAuthResult:
    connected = bool(
        getattr(account, "verified", True)
        and (getattr(account, "authenticated", False) or getattr(account, "connected", False))
    )
    label = str(getattr(account, "account_label", "") or "").strip()
    detail = str(getattr(account, "detail", "") or "").strip()
    if connected:
        summary = f"connected as {label}" if label else "connected"
        if detail and detail.casefold() not in summary.casefold():
            summary += f" ({detail})"
        return _DelegatedAuthResult(connected=True, summary=summary)
    summary = "not connected"
    if detail:
        summary += f" ({detail})"
    return _DelegatedAuthResult(connected=False, summary=summary)


def _check_delegated_runtime_connection(
    cfg: AppConfig,
    execution_result: _ExecutionStepResult,
) -> _DelegatedAuthResult:
    """Read the provider runtime's opaque account status without accessing credentials."""

    runtime_id = _delegated_runtime_id(execution_result)
    try:
        if _is_direct_subscription(runtime_id):
            from ..provider_auth import create_provider_auth

            return _delegated_auth_result(create_provider_auth(runtime_id).account_status())
        from ..agent_runtimes.service import runtime_connection_snapshot

        snapshot = runtime_connection_snapshot(cfg, runtime_id)
    except Exception as exc:  # noqa: BLE001 - setup must survive unavailable runtimes
        return _DelegatedAuthResult(
            connected=False,
            summary=f"not connected (status check failed: {exc})",
        )
    return _delegated_auth_result(snapshot.account)


def _login_delegated_runtime(
    cfg: AppConfig,
    execution_result: _ExecutionStepResult,
) -> _DelegatedAuthResult:
    """Run provider-owned browser login and return only its opaque account status."""

    runtime_id = _delegated_runtime_id(execution_result)
    try:
        if _is_direct_subscription(runtime_id):
            from ..provider_auth import create_provider_auth

            adapter = create_provider_auth(runtime_id)
            account = adapter.login(method="browser")
            if account.connected:
                _sync_direct_subscription_model(cfg, runtime_id, adapter=adapter)
            return _delegated_auth_result(account)
        from ..agent_runtimes.service import login_runtime

        account = login_runtime(cfg, runtime_id, method_id="browser")
    except Exception as exc:  # noqa: BLE001 - login is optional after config was saved
        return _DelegatedAuthResult(
            connected=False,
            summary=f"not connected ({exc})",
        )
    return _delegated_auth_result(account)


def _maybe_connect_delegated_runtime(
    *,
    console: Console,
    cfg: AppConfig,
    execution_result: _ExecutionStepResult,
) -> _DelegatedAuthResult:
    runtime_id = _delegated_runtime_id(execution_result)
    auth_result = _check_delegated_runtime_connection(cfg, execution_result)
    if auth_result.connected:
        if _is_direct_subscription(runtime_id):
            try:
                _sync_direct_subscription_model(cfg, runtime_id)
            except Exception as exc:  # noqa: BLE001
                return _DelegatedAuthResult(
                    connected=False,
                    summary=f"connected, but model discovery failed ({exc})",
                )
        console.print(f"[green]Subscription account already {auth_result.summary}.[/green]")
        return auth_result

    console.print()
    console.print(
        "[yellow]Provider login is an immediate external side effect. "
        "Cancelling or going back later does not undo it.[/yellow]"
    )
    try:
        connect = _prompt_yes_no("Connect now? [Y/n]", default=True)
    except (_GoBack, KeyboardInterrupt, Abort, EOFError):
        console.print(
            "[yellow]Your model access settings are already saved; provider sign-in was skipped.[/yellow]"
        )
        console.print(f"[dim]Run `alysis auth login {runtime_id}` whenever you're ready.[/dim]")
        return auth_result
    if not connect:
        console.print(f"[dim]Run `alysis auth login {runtime_id}` whenever you're ready.[/dim]")
        return auth_result

    console.print("[dim]Starting the provider's browser login…[/dim]")
    auth_result = _login_delegated_runtime(cfg, execution_result)
    if auth_result.connected:
        console.print(f"[green]Subscription account {auth_result.summary}.[/green]")
    else:
        console.print(f"[yellow]Subscription account {auth_result.summary}.[/yellow]")
        console.print(f"[dim]Finish later with `alysis auth login {runtime_id}`.[/dim]")
    return auth_result


def _snapshot_file(path: Path) -> _FileSnapshot:
    try:
        if path.exists():
            return _FileSnapshot(path=path, existed=True, data=path.read_bytes())
        return _FileSnapshot(path=path, existed=False, data=None)
    except OSError as exc:
        raise ConfigError(f"Failed to snapshot {path}: {exc}") from exc


def _rollback_partial_persistence(snapshots: list[_FileSnapshot]) -> list[str]:
    errors: list[str] = []
    for snapshot in snapshots:
        try:
            if snapshot.existed:
                snapshot.path.parent.mkdir(parents=True, exist_ok=True)
                snapshot.path.write_bytes(snapshot.data or b"")
            elif snapshot.path.exists():
                snapshot.path.unlink()
        except OSError as exc:
            errors.append(f"{snapshot.path}: {exc}")
    return errors


def _prompt_and_check_sandbox(console: Console, cfg: AppConfig) -> _SandboxStepResult:
    console.print()
    console.print(
        Panel(
            "Alysis Code runs shell commands in a sandbox to keep your system safe.\n"
            "Checking sandbox readiness...",
            title="Sandbox",
            border_style="cyan",
        )
    )
    try:
        result = diagnose_sandbox(cfg, include_smoke=False)
    except (_GoBack, KeyboardInterrupt, Abort, EOFError):
        console.print(
            "[yellow]Sandbox skipped. You can finish this later with `alysis sandbox setup`.[/yellow]"
        )
        return _SandboxStepResult(ready=False, status="skipped")
    except Exception as exc:  # noqa: BLE001 - sandbox backends fail in environment-specific ways
        console.print(
            f"[yellow]Sandbox check failed:[/yellow] {exc}. Re-run with `alysis sandbox doctor`."
        )
        return _SandboxStepResult(ready=False, status="check failed")

    _print_sandbox_status(console, result)
    if result.ready:
        return _SandboxStepResult(ready=True, status=result.selected_backend or result.status)

    console.print(Panel(_sandbox_problem_text(result), title="Sandbox", border_style="yellow"))
    if result.can_pull:
        try:
            should_pull = _prompt_yes_no(
                "Sandbox image not found. Pull it now? (~50MB) [Y/n]", default=True
            )
        except (_GoBack, KeyboardInterrupt, Abort, EOFError):
            console.print(
                "[yellow]Sandbox skipped. You can finish this later with `alysis sandbox setup`.[/yellow]"
            )
            return _SandboxStepResult(ready=False, status="skipped")
        if should_pull:
            try:
                return _pull_sandbox(console, cfg, result)
            except (_GoBack, KeyboardInterrupt, Abort, EOFError):
                console.print(
                    "[yellow]Sandbox skipped. You can finish this later with `alysis sandbox setup`.[/yellow]"
                )
                return _SandboxStepResult(ready=False, status="skipped")
        console.print("[yellow]You can finish this later with `alysis sandbox setup`.[/yellow]")
        return _SandboxStepResult(ready=False, status="not ready")

    return _offer_sandbox_choice(console, cfg, result)


def _offer_sandbox_choice(
    console: Console, cfg: AppConfig, result: SandboxDiagnostic
) -> _SandboxStepResult:
    """No usable backend was found: let the user install one, opt out, or defer.

    The secure default stays strict; this only surfaces an explicit, discoverable
    opt-out so a brand-new user is not silently blocked on their first task.
    """
    del result  # diagnosis already printed; we only need the decision now
    plan = detect_bubblewrap_install_plan()
    rows: list[tuple[str, str, str]] = []
    if plan is not None:
        rows.append(
            (
                "install_bwrap",
                "Install bubblewrap now (keeps sandbox on)",
                f"Runs: {plan.display}",
            )
        )
    rows.append(
        (
            "disable",
            "Run without sandbox for now (less safe)",
            "Commands run on the host shell. Re-enable anytime in /config.",
        )
    )
    rows.append(
        (
            "later",
            "Decide later",
            "Finish with `alysis sandbox setup`.",
        )
    )
    try:
        selected = _run_wizard_picker(
            console=console,
            title="Sandbox",
            subtitle=(
                "No sandbox backend (bubblewrap or Docker) was found. How do you want to proceed?"
            ),
            rows=rows,
            current_value=rows[0][0],
            cancel_hint="skip",
        )
    except (KeyboardInterrupt, Abort, EOFError):
        console.print(
            "[yellow]Sandbox skipped. You can finish this later with `alysis sandbox setup`.[/yellow]"
        )
        return _SandboxStepResult(ready=False, status="skipped")

    if selected == "install_bwrap":
        return _install_bwrap_and_recheck(console, cfg, plan)
    if selected == "disable":
        return _disable_sandbox(console, cfg)
    console.print("[yellow]You can finish this later with `alysis sandbox setup`.[/yellow]")
    return _SandboxStepResult(ready=False, status="not ready")


def _disable_sandbox(console: Console, cfg: AppConfig) -> _SandboxStepResult:
    apply_sandbox_mode_to_config(cfg, "off")
    try:
        save_config(cfg)
    except (ConfigError, OSError) as exc:
        console.print(f"[yellow]Could not save the sandbox setting:[/yellow] {exc}")
        return _SandboxStepResult(ready=False, status="not ready")
    console.print(
        "[yellow]Sandbox disabled - Alysis Code will run commands directly on the host shell. "
        "Re-enable anytime with /config -> Sandbox.[/yellow]"
    )
    return _SandboxStepResult(ready=False, status="disabled")


def _install_bwrap_and_recheck(
    console: Console, cfg: AppConfig, plan: BubblewrapInstallPlan | None
) -> _SandboxStepResult:
    display = plan.display if plan is not None else "auto"
    console.print(f"[dim]Installing bubblewrap ({display})...[/dim]")
    console.print("[dim]You may be prompted for your sudo password.[/dim]")
    install_result = install_bubblewrap(plan=plan)
    if not install_result.ok:
        console.print(
            f"[yellow]Bubblewrap install did not complete:[/yellow] {install_result.detail}"
        )
        console.print(
            "[yellow]You can finish this later with `alysis sandbox setup`, "
            "or choose to run without sandbox in /config.[/yellow]"
        )
        return _SandboxStepResult(ready=False, status="not ready")
    console.print("[green]Bubblewrap installed.[/green]")
    try:
        recheck = diagnose_sandbox(cfg, include_smoke=True)
    except Exception as exc:  # noqa: BLE001 - sandbox backends fail in environment-specific ways
        console.print(
            f"[yellow]Sandbox check failed after install:[/yellow] {exc}. "
            "Re-run with `alysis sandbox doctor`."
        )
        return _SandboxStepResult(ready=False, status="check failed")
    _print_sandbox_status(console, recheck)
    if recheck.ready:
        return _SandboxStepResult(ready=True, status=recheck.selected_backend or recheck.status)
    console.print(Panel(_sandbox_problem_text(recheck), title="Sandbox", border_style="yellow"))
    console.print("[yellow]You can finish this later with `alysis sandbox setup`.[/yellow]")
    return _SandboxStepResult(ready=False, status="not ready")


def _pull_sandbox(
    console: Console,
    cfg: AppConfig,
    initial_result: SandboxDiagnostic,
) -> _SandboxStepResult:
    console.print("[dim]Pulling Alysis Code sandbox image...[/dim]")
    try:
        pull_result = pull_sandbox_images(timeout_s=900)
    except (KeyboardInterrupt, Abort, EOFError):
        raise
    except Exception as exc:  # noqa: BLE001
        log_path = _write_exception_log("sandbox_pull", exc)
        console.print(f"[yellow]Sandbox image pull failed:[/yellow] {exc}")
        if log_path is not None:
            console.print(f"[dim]Details saved to: {log_path}[/dim]")
        console.print("[yellow]You can finish this later with `alysis sandbox setup`.[/yellow]")
        return _SandboxStepResult(ready=False, status="pull failed")

    if not pull_result.ok:
        log_path = _write_sandbox_pull_log(pull_result)
        console.print(
            Panel(_sandbox_problem_text(initial_result), title="Sandbox", border_style="yellow")
        )
        console.print(
            f"[yellow]Sandbox image pull failed:[/yellow] {pull_result.error or 'image pull failed'}"
        )
        if log_path is not None:
            console.print(f"[dim]Raw pull output saved to: {log_path}[/dim]")
        console.print("[yellow]You can finish this later with `alysis sandbox setup`.[/yellow]")
        return _SandboxStepResult(ready=False, status="pull failed")

    console.print("[green]Sandbox image pulled.[/green]")
    try:
        final_result = diagnose_sandbox(cfg, include_smoke=True)
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]Sandbox check failed after pull:[/yellow] {exc}. "
            "Re-run with `alysis sandbox doctor`."
        )
        return _SandboxStepResult(ready=False, status="check failed")
    _print_sandbox_status(console, final_result)
    if final_result.ready:
        return _SandboxStepResult(
            ready=True, status=final_result.selected_backend or final_result.status
        )
    console.print(
        Panel(_sandbox_problem_text(final_result), title="Sandbox", border_style="yellow")
    )
    console.print("[yellow]You can finish this later with `alysis sandbox setup`.[/yellow]")
    return _SandboxStepResult(ready=False, status="not ready")


def _print_sandbox_status(console: Console, result: SandboxDiagnostic) -> None:
    backend = result.selected_backend or result.status
    console.print(f"Backend detected: {backend}")
    console.print(f"Image present: {result.docker_image}")
    if result.ready:
        console.print("[green]Sandbox ready[/green]")


def _sandbox_problem_text(result: SandboxDiagnostic) -> str:
    return format_sandbox_problem_message(result)


def _print_setup_complete(
    *,
    console: Console,
    profile_result: _ProfileStepResult,
    api_key_result: _ApiKeyStepResult,
    model_result: _ModelStepResult,
    workspace_result: _WorkspaceStepResult,
    sandbox_result: _SandboxStepResult,
) -> None:
    api_key_label = _api_key_summary_label(api_key_result)
    sandbox_label = _sandbox_summary_label(sandbox_result)
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("label", no_wrap=True, style="bold")
    table.add_column("value")
    table.add_row("Profile:", f"{profile_result.label} (active)")
    table.add_row("API key:", api_key_label)
    table.add_row("Model:", model_result.model)
    if (
        profile_result.preset is not None
        and profile_result.preset.key in _MODEL_REASONING_SETUP_PRESET_KEYS
    ):
        table.add_row("Reasoning:", model_result.reasoning_label)
    table.add_row("Workspace:", workspace_result.workspace)
    table.add_row("Sandbox:", sandbox_label)
    console.print()
    console.print(
        Panel(
            Group(table, Text(""), Text("Type your first message to start chatting.", style="dim")),
            title="Setup complete",
            border_style="green",
        )
    )


def _print_delegated_setup_complete(
    *,
    console: Console,
    execution_result: _ExecutionStepResult,
    workspace_result: _WorkspaceStepResult,
    auth_result: _DelegatedAuthResult,
    cfg: AppConfig | None = None,
) -> None:
    runtime_id = str(execution_result.runtime or "").strip()
    auth_hint = execution_result.auth_hint
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("label", no_wrap=True, style="bold")
    table.add_column("value")
    table.add_row("Connection:", "AI subscription")
    table.add_row("Subscription:", execution_result.label)
    table.add_row("Workspace:", workspace_result.workspace)
    if cfg is not None and cfg.execution.backend == "native":
        if active_subscription_selection_ready(cfg):
            active = get_profile(cfg, str(cfg.extra_fields.get("active_profile") or ""))
            table.add_row("Model:", active.default_model if active is not None else cfg.model)
            if active is not None and active.reasoning_effort is not None:
                table.add_row("Reasoning:", active.reasoning_effort)
        else:
            table.add_row("Model setup:", "Choose model and reasoning in /config")
    table.add_row(
        "Authentication:",
        Text(auth_result.summary, style="green" if auth_result.connected else "yellow"),
    )
    next_step = (
        Text("Your AI subscription is connected.", style="dim")
        if auth_result.connected
        else Text(
            f"Next: run `alysis auth login {runtime_id}` before using this subscription.",
            style="yellow",
        )
    )
    console.print()
    console.print(
        Panel(
            Group(
                table,
                Text(""),
                next_step,
                Text(auth_hint, style="dim") if auth_hint else Text(""),
                Text(
                    "Use /config → Default Model to choose the subscription model and reasoning effort.",
                    style="dim",
                ),
            ),
            title="Setup complete",
            border_style="green",
        )
    )


def _is_direct_subscription(runtime_id: str) -> bool:
    from ..provider_auth import provider_auth_ids

    return str(runtime_id or "").strip() in provider_auth_ids()


def _direct_subscription_profile(cfg: AppConfig, runtime_id: str) -> ProfileSpec:
    if not _is_direct_subscription(runtime_id):
        raise ConfigError(f"No native subscription profile is registered for {runtime_id!r}.")
    from ..provider_auth import create_provider_auth

    adapter = create_provider_auth(runtime_id)
    existing = get_profile(cfg, adapter.profile_name)
    if existing is not None and existing.auth_provider == runtime_id:
        return ProfileSpec(
            name=existing.name,
            protocol=adapter.protocol,
            base_url=adapter.base_url,
            api_key_env=existing.api_key_env,
            auth_provider=runtime_id,
            extra_headers=dict(existing.extra_headers),
            default_model=existing.default_model,
            reasoning_effort=existing.reasoning_effort,
            reasoning_trace_adapter=existing.reasoning_trace_adapter,
            web_search_adapter=existing.web_search_adapter,
            web_search_model=existing.web_search_model,
            notes=existing.notes,
            cache_capability=existing.cache_capability,
        )
    return ProfileSpec(
        name=adapter.profile_name,
        protocol=adapter.protocol,
        base_url=adapter.base_url,
        auth_provider=runtime_id,
        notes=f"{adapter.display_name}. Uses Alysis Code's native agent loop.",
    )


def _sync_direct_subscription_model(
    cfg: AppConfig,
    runtime_id: str,
    *,
    adapter: Any | None = None,
) -> None:
    """Refresh the catalog and preserve only a /config-confirmed selection."""

    if not _is_direct_subscription(runtime_id):
        return
    from ..provider_auth import create_provider_auth

    resolved_adapter = adapter or create_provider_auth(runtime_id)
    models = resolved_adapter.list_models(refresh=True)
    if not models:
        raise ConfigError("The connected subscription account did not advertise any models.")
    profile_name = str(getattr(resolved_adapter, "profile_name", "") or "").strip()
    if not profile_name:
        raise ConfigError(f"Subscription adapter {runtime_id!r} has no profile name.")
    profile = get_profile(cfg, profile_name)
    if profile is None:
        raise ConfigError(f"Subscription profile {profile_name!r} is missing.")
    confirmation_pending = str(
        cfg.extra_fields.get(SUBSCRIPTION_SELECTION_REQUIRED_KEY) or ""
    ).strip().lower() in {"true", runtime_id}
    selection_ready = subscription_selection_supported(profile, models) and not confirmation_pending
    cfg.extra_fields[_ONBOARDED_KEY] = selection_ready
    cfg.extra_fields.pop("subscription_reconnect_required", None)
    if not selection_ready:
        cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = runtime_id
    save_config(cfg)


def _api_key_summary_label(api_key_result: _ApiKeyStepResult) -> Text:
    if not api_key_result.api_key:
        return Text("not stored", style="yellow")

    reason = str(api_key_result.validation_message or "").strip()
    if api_key_result.validation_status == "validated":
        return Text("stored, validated", style="green")
    if api_key_result.validation_status == "inconclusive":
        suffix = f" ({reason})" if reason else ""
        return Text(f"stored, validation inconclusive{suffix}", style="yellow")
    if api_key_result.validation_status == "failed":
        suffix = f" ({reason})" if reason else ""
        return Text(
            f"stored, but provider rejected it{suffix}. Re-enter via /config.",
            style="red",
        )
    if api_key_result.validation_status == "skipped":
        return Text("stored, validation skipped", style="yellow")
    if api_key_result.validation_status == "model_not_found":
        suffix = f" ({reason})" if reason else ""
        return Text(f"stored, model validation failed{suffix}", style="red")
    return Text(
        f"stored, unknown validation status: {api_key_result.validation_status}", style="red"
    )


def _sandbox_summary_label(sandbox_result: _SandboxStepResult) -> Text:
    if sandbox_result.ready:
        return Text(f"ready ({sandbox_result.status})", style="green")
    if sandbox_result.status == "disabled":
        return Text("off - host execution (less safe); re-enable via /config", style="yellow")
    if sandbox_result.status == "skipped":
        return Text("skipped - run `alysis sandbox setup` to finish", style="yellow")
    return Text(
        f"{sandbox_result.status} - run `alysis sandbox setup` to finish",
        style="yellow",
    )


def _run_wizard_picker(
    *,
    console: Console,
    title: str,
    subtitle: str,
    rows: list[tuple[str, str, str]],
    current_value: str,
    cancel_hint: str,
    invalid_hint: str = "Choose one of the numbered options.",
) -> str | None:
    if not rows:
        return None
    footer_hint = (
        f"↑/↓ navigate  Enter select  1-{min(len(rows), 9)} jump  Esc {cancel_hint}  Ctrl+C cancel"
    )
    try:
        from .config_menu import _run_config_picker
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim]Interactive picker unavailable: {exc}. Using numeric input.[/dim]")
        return _run_wizard_picker_fallback(
            console=console,
            title=title,
            subtitle=subtitle,
            rows=rows,
            current_value=current_value,
            footer_hint=footer_hint,
            invalid_hint=invalid_hint,
        )
    try:
        return _run_config_picker(
            console=console,
            title=title,
            subtitle=subtitle,
            rows=rows,
            current_value=current_value,
            footer_hint=footer_hint,
        )
    except (KeyboardInterrupt, Abort, EOFError):
        raise
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim]Interactive picker unavailable: {exc}. Using numeric input.[/dim]")
        return _run_wizard_picker_fallback(
            console=console,
            title=title,
            subtitle=subtitle,
            rows=rows,
            current_value=current_value,
            footer_hint=footer_hint,
            invalid_hint=invalid_hint,
        )


def _run_wizard_picker_fallback(
    *,
    console: Console,
    title: str,
    subtitle: str,
    rows: list[tuple[str, str, str]],
    current_value: str,
    footer_hint: str,
    invalid_hint: str,
) -> str | None:
    current = str(current_value or "")
    while True:
        console.print()
        console.rule(f"[bold]{title}[/bold]")
        console.print(f"[dim]{subtitle}[/dim]")
        console.print()
        for index, (value, label, description) in enumerate(rows, start=1):
            suffix = " [dim](current)[/dim]" if value == current else ""
            detail = f"  [dim]{description}[/dim]" if description else ""
            console.print(f"  {index}) {label}{suffix}{detail}")
        console.print()
        console.print(f"[dim]{footer_hint}[/dim]")
        raw = _prompt_text("Choice", default="", show_default=False).strip()
        if not raw or _is_cancel_token(raw):
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(rows):
                return rows[idx][0]
        matched = _match_row_by_key_or_label(rows, raw)
        if matched is not None:
            return matched
        console.print(f"[yellow]{invalid_hint}[/yellow]")


def _match_row_by_key_or_label(rows: list[tuple[str, str, str]], raw_value: str) -> str | None:
    value = str(raw_value or "").strip().casefold()
    if not value:
        return None
    for row_value, label, _description in rows:
        if value in {row_value.casefold(), label.casefold()}:
            return row_value
    return None


def _setup_presets() -> list[ProfilePreset]:
    return provider_selection_presets()


def _advanced_setup_presets() -> list[ProfilePreset]:
    return advanced_provider_selection_presets()


def _provider_picker_rows(presets: list[ProfilePreset]) -> list[tuple[str, str, str]]:
    rows = [
        (preset.key, preset_selection_label(preset), _preset_description(preset))
        for preset in presets
    ]
    rows.append(
        (
            _ADVANCED_PROVIDER_PRESETS_VALUE,
            "Advanced / local / compatibility providers",
            "Show local endpoints (Ollama, LM Studio, vLLM), custom URLs, and legacy "
            "OpenAI-compatible first-party presets.",
        )
    )
    return rows


def _preset_by_key(key: str) -> ProfilePreset | None:
    normalized = str(key or "").strip().lower()
    for preset in PROFILE_PRESETS:
        if preset.key == normalized:
            return preset
    return None


def _preset_description(preset: ProfilePreset) -> str:
    prefix = preset_protocol_summary(preset)
    if preset.notes:
        return f"{prefix}. {preset.notes}"
    parsed = urlparse(preset.base_url)
    if parsed.netloc:
        return f"{prefix}. Host: {parsed.netloc}"
    return f"{prefix}. Use any OpenAI-compatible base URL"


def _print_preset_warning(console: Console, preset: ProfilePreset) -> None:
    warning = str(preset.setup_warning or "").strip()
    if warning:
        console.print(f"[yellow]{warning}[/yellow]")


def _print_provider_diagnostic_warnings(console: Console, cfg: AppConfig) -> None:
    try:
        issues = provider_diagnostic_warning_lines(cfg)
    except ConfigError:
        return
    for issue in issues:
        console.print(f"[yellow]Provider diagnostic: {issue}[/yellow]")


def _suggest_workspace_default() -> Path:
    home = Path.home()
    for candidate in (home / "projects", home / "code"):
        if candidate.exists() and candidate.is_dir():
            return candidate
    return home


def _display_path(path: Path) -> str:
    home = Path.home()
    if path == home:
        return "~"
    if path.is_relative_to(home):
        return "~" + os.fspath(path).removeprefix(os.fspath(home))
    return os.fspath(path)


def _escape_capture_unavailable_reason() -> str | None:
    try:
        from ..cli import _is_non_interactive_terminal
    except Exception as exc:  # noqa: BLE001 - capability checks should degrade clearly
        return f"terminal capability check failed: {exc}"
    if _is_non_interactive_terminal():
        return "non-interactive terminal"
    try:
        from prompt_toolkit import PromptSession  # noqa: F401
        from prompt_toolkit.key_binding import KeyBindings  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"prompt_toolkit unavailable: {exc}"
    return None


def _can_capture_escape() -> bool:
    return _escape_capture_unavailable_reason() is None


def _esc_aware_text_input(
    prompt: str,
    *,
    default: str = "",
    show_default: bool = True,
    hide_input: bool = False,
) -> str:
    if not _can_capture_escape():
        return _prompt_text(
            prompt,
            default=default,
            show_default=show_default,
            hide_input=hide_input,
        )
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
    except Exception as exc:  # noqa: BLE001
        _resolve_console().print(
            f"[dim]Esc/back navigation unavailable: {exc}. Using line input.[/dim]"
        )
        return _prompt_text(
            prompt,
            default=default,
            show_default=show_default,
            hide_input=hide_input,
        )

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def _go_back(event: Any) -> None:
        event.app.exit(exception=_GoBack())

    session: PromptSession[str] = PromptSession(
        key_bindings=bindings,
        is_password=hide_input,
    )
    try:
        return str(
            session.prompt(
                f"{prompt}: ",
                default=default,
                is_password=hide_input,
            )
        )
    except _GoBack:
        raise
    except (KeyboardInterrupt, EOFError):
        raise


def _confirm_cancel(
    console: Console, *, prompt: str = "Cancel setup and discard inputs? [y/N]"
) -> bool:
    while True:
        try:
            value = _esc_aware_text_input(prompt, default="n", show_default=False).strip().lower()
        except _GoBack:
            return False
        except (Abort, EOFError, KeyboardInterrupt):
            return True
        if not value:
            return False
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        console.print("[red]Enter y or n.[/red]")


def _prompt_text(
    prompt: str,
    *,
    default: str,
    show_default: bool,
    hide_input: bool = False,
) -> str:
    return str(
        typer.prompt(
            prompt,
            default=default,
            show_default=show_default,
            hide_input=hide_input,
        )
    )


def _prompt_yes_no(prompt: str, *, default: bool) -> bool:
    default_text = "y" if default else "n"
    while True:
        value = (
            _esc_aware_text_input(prompt, default=default_text, show_default=False).strip().lower()
        )
        if _is_cancel_token(value):
            raise _GoBack()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        _resolve_console().print("[red]Enter y or n.[/red]")


def _is_cancel_token(value: str) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {"\x1b", "esc", "escape", "cancel", "q", "quit"}


def _write_sandbox_pull_log(result: SandboxPullResult) -> Path | None:
    lines = [
        f"ok={result.ok}",
        f"error={result.error or ''}",
        "",
    ]
    for item in result.results:
        lines.extend(
            [
                f"image={item.image}",
                f"ok={item.ok}",
                "output:",
                item.output,
                "",
            ]
        )
    return _write_log("sandbox_pull", "\n".join(lines))


def _write_exception_log(prefix: str, exc: BaseException) -> Path | None:
    content = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return _write_log(prefix, content)


def _write_log(prefix: str, content: str) -> Path | None:
    try:
        logs_dir = default_sessions_dir().parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = logs_dir / f"{prefix}_{timestamp}.log"
        path.write_text(content, encoding="utf-8")
        return path
    except OSError as exc:
        _resolve_console().print(f"[yellow]Could not write diagnostic log:[/yellow] {exc}")
        return None
