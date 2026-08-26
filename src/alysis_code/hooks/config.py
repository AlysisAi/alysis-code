from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..branding import canonical_user_config_dir, env_get
from ..config import ConfigError
from .models import (
    CanonicalHookEventName,
    CommandHookSpec,
    HookConfigFile,
    HookEventName,
    canonicalize_hook_event_name,
)
from .trust import is_project_hooks_config_trusted, load_trust_state


def _hooks_config_dir() -> Path:
    override = env_get("ALYSIS_CONFIG_DIR")
    if override:
        return Path(override)
    return canonical_user_config_dir()


def user_hooks_config_path() -> Path:
    return _hooks_config_dir() / "hooks.json"


def project_hooks_config_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".alysis" / "hooks.json"


def project_local_hooks_config_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".alysis" / "hooks.local.json"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Failed to read hooks config: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid hooks config format (expected JSON object): {path}")
    return raw


def _format_validation_error(path: Path, exc: ValidationError) -> ConfigError:
    details: list[str] = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = str(err.get("msg") or "invalid value")
        detail = f"{path}: {msg}" if not loc else f"{path}: {loc}: {msg}"
        details.append(detail)
    joined = "\n".join(details) if details else f"{path}: invalid hooks config"
    return ConfigError(f"Invalid hooks config:\n{joined}")


def _load_hook_config_file(path: Path) -> HookConfigFile:
    raw = _read_json_object(path)
    try:
        return HookConfigFile.model_validate(raw)
    except ValidationError as exc:
        raise _format_validation_error(path, exc) from exc


def load_hook_config_file(path: Path) -> HookConfigFile:
    return _load_hook_config_file(path)


@dataclass(frozen=True)
class ResolvedHookMatcherGroup:
    event_name: CanonicalHookEventName
    matcher: str
    hooks: tuple[CommandHookSpec, ...]
    source_path: Path
    source_scope: str
    trusted: bool = True


@dataclass(frozen=True)
class ResolvedHookConfig:
    groups_by_event: dict[str, tuple[ResolvedHookMatcherGroup, ...]] = field(default_factory=dict)
    loaded_paths: tuple[Path, ...] = ()
    untrusted_project_paths: tuple[Path, ...] = ()

    @property
    def has_any_hooks(self) -> bool:
        return any(groups for groups in self.groups_by_event.values())

    def groups_for_event(self, event_name: HookEventName) -> tuple[ResolvedHookMatcherGroup, ...]:
        return self.groups_by_event.get(canonicalize_hook_event_name(event_name), ())


def _resolve_groups_for_file(
    *,
    config_file: HookConfigFile,
    source_path: Path,
    source_scope: str,
    trusted: bool,
) -> dict[str, list[ResolvedHookMatcherGroup]]:
    resolved: dict[str, list[ResolvedHookMatcherGroup]] = {}
    for event_name, groups in config_file.hooks.items():
        event_groups: list[ResolvedHookMatcherGroup] = []
        for group in groups:
            if not group.enabled:
                continue
            enabled_hooks = tuple(
                sorted(
                    (hook for hook in group.hooks if hook.enabled),
                    key=lambda hook: (-hook.priority, hook.id or "", hook.command),
                )
            )
            if not enabled_hooks:
                continue
            event_groups.append(
                ResolvedHookMatcherGroup(
                    event_name=event_name,
                    matcher=str(group.matcher or ""),
                    hooks=enabled_hooks,
                    source_path=source_path,
                    source_scope=source_scope,
                    trusted=trusted,
                )
            )
        if event_groups:
            resolved[event_name] = event_groups
    return resolved


def _drop_overridden_hooks(
    groups: list[ResolvedHookMatcherGroup],
    *,
    overridden_ids: set[str],
) -> list[ResolvedHookMatcherGroup]:
    if not overridden_ids:
        return groups
    retained: list[ResolvedHookMatcherGroup] = []
    for group in groups:
        remaining_hooks = tuple(hook for hook in group.hooks if hook.id not in overridden_ids)
        if not remaining_hooks:
            continue
        if remaining_hooks == group.hooks:
            retained.append(group)
            continue
        retained.append(
            ResolvedHookMatcherGroup(
                event_name=group.event_name,
                matcher=group.matcher,
                hooks=remaining_hooks,
                source_path=group.source_path,
                source_scope=group.source_scope,
                trusted=group.trusted,
            )
        )
    return retained


def _merge_event_groups(
    *,
    existing: list[ResolvedHookMatcherGroup],
    incoming: list[ResolvedHookMatcherGroup],
) -> list[ResolvedHookMatcherGroup]:
    override_ids = {hook.id for group in incoming for hook in group.hooks if hook.id is not None}
    merged = _drop_overridden_hooks(existing, overridden_ids=override_ids)
    merged.extend(incoming)
    return merged


def load_resolved_hooks_config(workspace_root: Path) -> ResolvedHookConfig:
    workspace_root = workspace_root.resolve()
    candidate_paths = (
        (user_hooks_config_path(), "user"),
        (project_hooks_config_path(workspace_root), "project"),
        (project_local_hooks_config_path(workspace_root), "project_local"),
    )
    trust_state = load_trust_state()
    merged: dict[str, list[ResolvedHookMatcherGroup]] = {}
    loaded_paths: list[Path] = []
    untrusted_project_paths: list[Path] = []
    for path, source_scope in candidate_paths:
        if not path.exists():
            continue
        trusted = source_scope != "project" or is_project_hooks_config_trusted(
            workspace_root=workspace_root,
            config_path=path,
            state=trust_state,
        )
        loaded_paths.append(path)
        if not trusted:
            untrusted_project_paths.append(path)
            continue
        config_file = _load_hook_config_file(path)
        resolved = _resolve_groups_for_file(
            config_file=config_file,
            source_path=path,
            source_scope=source_scope,
            trusted=trusted,
        )
        if not resolved:
            continue
        for event_name, groups in resolved.items():
            merged[event_name] = _merge_event_groups(
                existing=merged.get(event_name, []),
                incoming=groups,
            )
    return ResolvedHookConfig(
        groups_by_event={event_name: tuple(groups) for event_name, groups in merged.items()},
        loaded_paths=tuple(loaded_paths),
        untrusted_project_paths=tuple(untrusted_project_paths),
    )


__all__ = [
    "ResolvedHookConfig",
    "ResolvedHookMatcherGroup",
    "load_hook_config_file",
    "load_resolved_hooks_config",
    "project_hooks_config_path",
    "project_local_hooks_config_path",
    "user_hooks_config_path",
]
