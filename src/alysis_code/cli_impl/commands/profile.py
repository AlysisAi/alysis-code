from __future__ import annotations

import json
import os
import sys
from typing import Any

import typer

from ...config import (
    AppConfig,
    ConfigError,
    clear_persisted_profile_key,
    load_config,
    rename_persisted_profile_key,
    resolve_profile_api_key,
    save_config,
    save_persisted_profile_key,
)
from . import _patchable
from ._shared import _console, _Table


def _cli_module() -> Any:
    module = sys.modules.get("alysis_code.cli")
    if module is not None:
        return module
    from ... import cli

    return cli


profile_app = typer.Typer(add_completion=False, help="Provider profile commands.")


def _ensure_generic_profile_mutation_allowed(
    cfg: AppConfig,
    name: str,
    *,
    operation: str,
) -> None:
    """Keep generic profile commands from taking ownership of auth profiles."""

    from ...profiles import get_profile

    existing = get_profile(cfg, name)
    if existing is None or not existing.auth_provider:
        return
    raise ConfigError(
        f"Profile {existing.name!r} is managed by the {existing.auth_provider!r} "
        f"subscription connection and cannot be {operation} through generic profile "
        "commands. Choose a different profile name or manage the connection through auth."
    )


def _parse_profile_headers(header_values: list[str] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_value in header_values or []:
        text = str(raw_value or "").strip()
        if not text:
            continue
        if "=" not in text:
            raise ConfigError("--header values must use k=v syntax.")
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ConfigError("--header values must use non-empty k=v syntax.")
        headers[key] = value
    return headers


def _profile_api_key_status(cfg: AppConfig, profile_name: str) -> str:
    resolved = resolve_profile_api_key(cfg, profile_name)
    if not resolved.key:
        return "missing"
    return _cli_module()._api_key_source_label(resolved.source)


@profile_app.command("list")
def profile_list() -> None:
    """List provider profiles."""
    from ...profiles import list_profiles

    console = _console()
    cfg = _patchable("load_config", load_config)()
    active = str((cfg.extra_fields or {}).get("active_profile") or "")
    table = _Table(title="Provider Profiles")
    table.add_column("name")
    table.add_column("active")
    table.add_column("protocol")
    table.add_column("base_url")
    table.add_column("api_key")
    table.add_column("default_model")
    table.add_column("web_search")
    for profile in list_profiles(cfg):
        table.add_row(
            profile.name,
            "✓" if profile.name == active else "",
            profile.protocol,
            profile.base_url,
            _profile_api_key_status(cfg, profile.name),
            profile.default_model,
            profile.web_search_adapter,
        )
    console.print(table)


@profile_app.command("show")
def profile_show(name: str = typer.Argument(..., help="Profile name.")) -> None:
    """Show one provider profile."""
    from ...profiles import get_profile

    console = _console()
    cfg = _patchable("load_config", load_config)()
    profile = get_profile(cfg, name)
    if profile is None:
        console.print(f"[red]Profile not found:[/red] {name}")
        raise typer.Exit(code=2)
    data = profile.to_dict()
    data["name"] = profile.name
    data["active"] = profile.name == str((cfg.extra_fields or {}).get("active_profile") or "")
    data["api_key"] = _profile_api_key_status(cfg, profile.name)
    console.print_json(json.dumps(data, indent=2, sort_keys=True))


@profile_app.command("add")
def profile_add(
    name: str = typer.Argument(..., help="Profile name."),
    base_url: str = typer.Option(..., "--base-url", help="OpenAI-compatible base URL."),
    api_key_env: str | None = typer.Option(None, "--api-key-env", help="API key env var."),
    header: list[str] | None = typer.Option(None, "--header", help="Extra request header k=v."),
    default_model: str = typer.Option("", "--default-model", help="Default model."),
    reasoning_trace_adapter: str = typer.Option(
        "auto",
        "--reasoning-trace-adapter",
        help="Safe reasoning-summary adapter for this provider profile.",
    ),
    web_search_adapter: str = typer.Option(
        "auto",
        "--web-search-adapter",
        help="web_search adapter for this profile.",
    ),
    web_search_model: str = typer.Option(
        "",
        "--web-search-model",
        help="Optional model used only by web_search.",
    ),
    notes: str = typer.Option("", "--notes", help="Notes."),
    protocol: str = typer.Option("openai_compat", "--protocol", help="Profile protocol."),
) -> None:
    """Add a custom provider profile."""
    from ...profiles import ProfileSpec, add_profile

    console = _console()
    cfg = _patchable("load_config", load_config)()
    try:
        _ensure_generic_profile_mutation_allowed(cfg, name, operation="overwritten")
        profile = ProfileSpec(
            name=name,
            protocol=protocol,
            base_url=base_url,
            api_key_env=api_key_env,
            extra_headers=_parse_profile_headers(header),
            default_model=default_model,
            reasoning_trace_adapter=reasoning_trace_adapter,
            web_search_adapter=web_search_adapter,
            web_search_model=web_search_model,
            notes=notes,
        )
        add_profile(cfg, profile)
        save_config(cfg)
    except ConfigError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"Added profile {profile.name}.")


@profile_app.command("remove")
def profile_remove(
    name: str = typer.Argument(..., help="Profile name."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Remove a provider profile."""
    from ...profiles import remove_profile

    console = _console()
    if not yes and not typer.confirm(f"Remove profile {name}?", default=False):
        console.print("Cancelled.")
        return
    cfg = _patchable("load_config", load_config)()
    try:
        remove_profile(cfg, name)
        clear_persisted_profile_key(name)
        save_config(cfg)
    except ConfigError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"Removed profile {name}.")


@profile_app.command("use")
def profile_use(name: str = typer.Argument(..., help="Profile name.")) -> None:
    """Switch the active provider profile."""
    from ...profiles import set_active_profile

    console = _console()
    cfg = _patchable("load_config", load_config)()
    try:
        set_active_profile(cfg, name)
        save_config(cfg)
    except ConfigError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"Active profile: {name}")


@profile_app.command("rename")
def profile_rename(
    old: str = typer.Argument(..., help="Current profile name."),
    new: str = typer.Argument(..., help="New profile name."),
) -> None:
    """Rename a provider profile."""
    from ...profiles import rename_profile

    console = _console()
    cfg = _patchable("load_config", load_config)()
    try:
        rename_profile(cfg, old, new)
        rename_persisted_profile_key(old, new)
        save_config(cfg)
    except ConfigError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"Renamed profile {old} to {new}.")


@profile_app.command("preset")
def profile_preset(
    preset_key: str = typer.Argument(..., help="Preset key."),
    profile_name_as: str | None = typer.Option(None, "--as", help="Profile name to create."),
    api_key: str | None = typer.Option(None, "--api-key", help="API key to store."),
    api_key_stdin: bool = typer.Option(False, "--api-key-stdin", help="Read API key from stdin."),
    yes: bool = typer.Option(False, "--yes", help="Overwrite without confirmation."),
) -> None:
    """Clone a provider preset into a profile."""
    from ...profile_presets import get_preset, make_profile_from_preset
    from ...profiles import add_profile, get_profile, update_profile

    console = _console()
    preset = get_preset(preset_key)
    if preset is None:
        console.print(f"[red]Unknown preset:[/red] {preset_key}")
        raise typer.Exit(code=2)
    cfg = _patchable("load_config", load_config)()
    name = str(profile_name_as or preset.key).strip().lower()
    existing = get_profile(cfg, name)
    try:
        _ensure_generic_profile_mutation_allowed(cfg, name, operation="overwritten")
    except ConfigError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if existing is not None and not yes:
        if not typer.confirm(f"Overwrite profile {name}?", default=False):
            console.print("Cancelled.")
            return
    base_url = preset.base_url
    if not base_url:
        base_url = str(typer.prompt("Base URL", default="", show_default=False)).strip()
    try:
        profile = make_profile_from_preset(preset, name=name)
        if base_url != profile.base_url:
            profile = make_profile_from_preset(preset, name=name)
            add_profile(cfg, profile)
            update_profile(cfg, profile.name, base_url=base_url)
        else:
            add_profile(cfg, profile)
        save_config(cfg)
        key_value = ""
        if api_key_stdin:
            key_value = sys.stdin.read().strip()
        elif api_key is not None:
            key_value = api_key.strip()
        if key_value:
            save_persisted_profile_key(profile.name, key_value)
    except ConfigError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"Added profile {name} from preset {preset.key}.")


@profile_app.command("convert")
def profile_convert(
    name: str | None = typer.Argument(
        None,
        help="Profile name. Defaults to the active profile.",
    ),
    target: str = typer.Option(
        ...,
        "--to",
        help="Conversion target: native or compatibility.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Convert a first-party profile between native and compatibility protocols."""
    from ...profile_presets import (
        convert_profile_to_preset,
        normalize_conversion_target,
        target_preset_for_profile_conversion,
    )
    from ...profiles import add_profile, get_profile, set_active_profile

    console = _console()
    cfg = _patchable("load_config", load_config)()
    profile_name = str(name or (cfg.extra_fields or {}).get("active_profile") or "").strip()
    if not profile_name:
        console.print(
            "[red]Profile error:[/red] No profile name supplied and no active profile set."
        )
        raise typer.Exit(code=2)
    profile = get_profile(cfg, profile_name)
    if profile is None:
        console.print(f"[red]Profile not found:[/red] {profile_name}")
        raise typer.Exit(code=2)
    try:
        _ensure_generic_profile_mutation_allowed(cfg, profile.name, operation="converted")
        normalized_target = normalize_conversion_target(target)
    except (ConfigError, ValueError) as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    preset = target_preset_for_profile_conversion(profile, target=normalized_target)
    if preset is None:
        console.print(
            "[red]Profile error:[/red] Only OpenAI, Anthropic, and Gemini profiles can be "
            "converted between native and compatibility protocols."
        )
        raise typer.Exit(code=2)
    converted = convert_profile_to_preset(profile, preset)
    if converted.to_dict() == profile.to_dict():
        console.print(
            f"Profile {profile.name} is already using the {normalized_target} preset "
            f"({preset.key})."
        )
        return

    table = _Table(title=f"Convert profile {profile.name}")
    table.add_column("field")
    table.add_column("before")
    table.add_column("after")
    table.add_row("preset", profile.name, preset.key)
    table.add_row("protocol", profile.protocol, converted.protocol)
    table.add_row("base_url", profile.base_url, converted.base_url)
    table.add_row(
        "default_model", profile.default_model or "(empty)", converted.default_model or "(empty)"
    )
    table.add_row("web_search_adapter", profile.web_search_adapter, converted.web_search_adapter)
    table.add_row(
        "web_search_model",
        profile.web_search_model or "(empty)",
        converted.web_search_model or "(empty)",
    )
    table.add_row(
        "api_key",
        _profile_api_key_status(cfg, profile.name),
        _profile_api_key_status(cfg, profile.name),
    )
    console.print(table)

    if not yes and not typer.confirm(
        f"Convert profile {profile.name} to {normalized_target}?", default=False
    ):
        console.print("Cancelled.")
        return

    add_profile(cfg, converted)
    if str((cfg.extra_fields or {}).get("active_profile") or "") == profile.name:
        set_active_profile(cfg, profile.name)
    save_config(cfg)
    console.print(f"Converted profile {profile.name} to {normalized_target} ({preset.key}).")


@profile_app.command("presets")
def profile_presets() -> None:
    """List available provider presets."""
    from ...profile_presets import PROFILE_PRESETS, preset_protocol_kind, preset_selection_label

    table = _Table(title="Provider Presets")
    table.add_column("key")
    table.add_column("label")
    table.add_column("kind")
    table.add_column("protocol")
    table.add_column("base_url")
    table.add_column("web_search")
    for preset in PROFILE_PRESETS:
        table.add_row(
            preset.key,
            preset_selection_label(preset),
            preset_protocol_kind(preset),
            preset.protocol,
            preset.base_url,
            preset.web_search_adapter,
        )
    _console().print(table)


@profile_app.command("set-key")
def profile_set_key(
    name: str = typer.Argument(..., help="Profile name."),
    stdin: bool = typer.Option(False, "--stdin", help="Read API key from stdin."),
    from_env: str | None = typer.Option(None, "--from-env", help="Copy key from env var."),
    key: str | None = typer.Option(None, "--key", help="Inline key; avoid shell history."),
) -> None:
    """Store an API key for one profile."""
    from ...profiles import get_profile

    console = _console()
    cfg = _patchable("load_config", load_config)()
    profile = get_profile(cfg, name)
    if profile is None:
        console.print(f"[red]Profile not found:[/red] {name}")
        raise typer.Exit(code=2)
    sources = sum(1 for value in (stdin, bool(from_env), key is not None) if value)
    if sources > 1:
        console.print("[red]Profile error:[/red] Use only one key source.")
        raise typer.Exit(code=2)
    if stdin:
        key_value = sys.stdin.read().strip()
    elif from_env:
        key_value = str(os.environ.get(from_env) or "").strip()
        if not key_value:
            console.print(f"[red]Profile error:[/red] Environment variable {from_env} is not set.")
            raise typer.Exit(code=2)
    elif key is not None:
        key_value = key.strip()
        console.print("[yellow]Warning:[/yellow] inline keys may be stored in shell history.")
    else:
        key_value = typer.prompt("API key", hide_input=True).strip()
    try:
        save_persisted_profile_key(profile.name, key_value)
    except ConfigError as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"Saved API key for profile {profile.name}.")


@profile_app.command("clear-key")
def profile_clear_key(name: str = typer.Argument(..., help="Profile name.")) -> None:
    """Remove a stored API key for one profile."""
    removed = clear_persisted_profile_key(name)
    if removed:
        _console().print(f"Removed API key for profile {name}.")
        return
    _console().print(f"No stored API key found for profile {name}.")
