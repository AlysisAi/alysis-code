from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import ProjectExtensionOverrides, normalize_extension_id
from .paths import project_extensions_path
from .state import compute_effective_enabled, load_global_state
from .workspace_trust import grant_workspace_trust, is_workspace_trusted


@dataclass(frozen=True)
class ActivationDecision:
    enabled_plugin_ids: frozenset[str]
    workspace_trust_was_prompted: bool
    workspace_trust_granted: bool
    untrusted_project_plugin_ids: frozenset[str]


class WorkspaceTrustPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_root: str
    overrides_sha256: str
    plugins_added: tuple[str, ...]
    plugins_removed: tuple[str, ...]


WorkspaceTrustPromptFn = Callable[[WorkspaceTrustPromptRequest], bool]


def resolve_active_plugins(
    *,
    repo_root: Path,
    workspace_trust_prompt: WorkspaceTrustPromptFn | None,
    user_config_dir: Path | None = None,
) -> ActivationDecision:
    global_state = load_global_state()
    empty_overrides = ProjectExtensionOverrides()
    baseline_enabled = frozenset(compute_effective_enabled(global_state, empty_overrides))

    overrides_path = project_extensions_path(repo_root)
    if not overrides_path.exists():
        return ActivationDecision(
            enabled_plugin_ids=baseline_enabled,
            workspace_trust_was_prompted=False,
            workspace_trust_granted=False,
            untrusted_project_plugin_ids=frozenset(),
        )

    raw_bytes = _read_overrides_bytes(overrides_path)
    raw = _decode_json_object(raw_bytes, overrides_path)
    project_overrides = ProjectExtensionOverrides.model_validate(raw)
    if not project_overrides.enabled and not project_overrides.disabled:
        return ActivationDecision(
            enabled_plugin_ids=baseline_enabled,
            workspace_trust_was_prompted=False,
            workspace_trust_granted=False,
            untrusted_project_plugin_ids=frozenset(),
        )

    overrides_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    trusted_enabled = frozenset(compute_effective_enabled(global_state, project_overrides))
    untrusted_ids = frozenset(trusted_enabled - baseline_enabled)
    if is_workspace_trusted(
        repo_root=repo_root,
        overrides_sha256=overrides_sha256,
        user_config_dir=user_config_dir,
    ):
        return ActivationDecision(
            enabled_plugin_ids=trusted_enabled,
            workspace_trust_was_prompted=False,
            workspace_trust_granted=True,
            untrusted_project_plugin_ids=frozenset(),
        )

    if workspace_trust_prompt is None:
        return ActivationDecision(
            enabled_plugin_ids=baseline_enabled,
            workspace_trust_was_prompted=False,
            workspace_trust_granted=False,
            untrusted_project_plugin_ids=untrusted_ids,
        )

    request = WorkspaceTrustPromptRequest(
        repo_root=str(repo_root.expanduser().resolve(strict=False)),
        overrides_sha256=overrides_sha256,
        plugins_added=_normalized_tuple(project_overrides.enabled),
        plugins_removed=_normalized_tuple(project_overrides.disabled),
    )
    granted = bool(workspace_trust_prompt(request))
    if granted:
        grant_workspace_trust(
            repo_root=repo_root,
            overrides_sha256=overrides_sha256,
            user_config_dir=user_config_dir,
        )
        return ActivationDecision(
            enabled_plugin_ids=trusted_enabled,
            workspace_trust_was_prompted=True,
            workspace_trust_granted=True,
            untrusted_project_plugin_ids=frozenset(),
        )

    return ActivationDecision(
        enabled_plugin_ids=baseline_enabled,
        workspace_trust_was_prompted=True,
        workspace_trust_granted=False,
        untrusted_project_plugin_ids=untrusted_ids,
    )


def _normalized_tuple(values: list[str]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := normalize_extension_id(value))})
    )


def _read_overrides_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"Failed to read {path}") from exc


def _decode_json_object(raw_bytes: bytes, path: Path) -> dict[str, object]:
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid JSON format in {path}: expected object.")
    return raw
