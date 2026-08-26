from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_json
from .models import ExtensionState, ProjectExtensionOverrides, normalize_extension_id
from .paths import extensions_state_path, project_extensions_path


def _load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {path}") from e
    except OSError as e:
        raise RuntimeError(f"Failed to read {path}") from e

    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid JSON format in {path}: expected object.")
    return raw


def load_global_state() -> ExtensionState:
    raw = _load_json_object(extensions_state_path())
    if raw is None:
        return ExtensionState()
    return ExtensionState.model_validate(raw)


def save_global_state(state: ExtensionState) -> None:
    atomic_write_json(extensions_state_path(), state.model_dump(mode="json"))


def load_project_state(repo_root: Path) -> ExtensionState:
    raw = _load_json_object(project_extensions_path(repo_root))
    if raw is None:
        return ExtensionState()
    return ExtensionState.model_validate(raw)


def save_project_state(repo_root: Path, state: ExtensionState) -> None:
    atomic_write_json(project_extensions_path(repo_root), state.model_dump(mode="json"))


def load_project_overrides(repo_root: Path) -> ProjectExtensionOverrides:
    raw = _load_json_object(project_extensions_path(repo_root))
    if raw is None:
        return ProjectExtensionOverrides()
    return ProjectExtensionOverrides.model_validate(raw)


def compute_effective_enabled(
    global_state: ExtensionState,
    project_overrides: ProjectExtensionOverrides,
) -> set[str]:
    enabled: set[str] = {
        normalize_extension_id(ext_id)
        for ext_id in global_state.enabled
        if normalize_extension_id(ext_id)
    }

    for ext_id, installed in global_state.installed.items():
        if installed.enabled:
            normalized = normalize_extension_id(ext_id)
            if normalized:
                enabled.add(normalized)

    for ext_id in project_overrides.disabled:
        normalized = normalize_extension_id(ext_id)
        if normalized:
            enabled.discard(normalized)

    for ext_id in project_overrides.enabled:
        normalized = normalize_extension_id(ext_id)
        if normalized:
            enabled.add(normalized)

    return enabled
