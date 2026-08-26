from __future__ import annotations

from pathlib import Path

from ..branding import canonical_user_config_dir, canonical_user_data_dir, env_get
from .models import plugin_slug_from_id


def _config_dir() -> Path:
    override = env_get("ALYSIS_CONFIG_DIR")
    if override:
        return Path(override)
    return canonical_user_config_dir()


def _data_dir() -> Path:
    override = env_get("ALYSIS_DATA_DIR")
    if override:
        return Path(override)
    return canonical_user_data_dir()


def extensions_data_dir() -> Path:
    return _data_dir() / "extensions"


def extensions_state_path() -> Path:
    return extensions_data_dir() / "state.json"


def extensions_cache_dir() -> Path:
    return extensions_data_dir() / "cache"


def project_extensions_path(repo_root: Path) -> Path:
    return repo_root.resolve() / ".alysis" / "extensions.json"


def workspace_trust_path(*, user_config_dir: Path | None = None) -> Path:
    if user_config_dir is not None:
        return user_config_dir.expanduser().resolve() / "extensions" / "workspace_trust.json"
    return extensions_data_dir() / "workspace_trust.json"


def installed_plugin_root(
    plugin_id: str,
    *,
    project: bool,
    repo_root: Path | None,
) -> Path:
    slug = plugin_slug_from_id(plugin_id)
    if project:
        if repo_root is None:
            raise ValueError("repo_root is required for project plugin installs.")
        return repo_root.resolve() / ".alysis" / "plugins" / slug
    return extensions_data_dir() / "installed" / slug
