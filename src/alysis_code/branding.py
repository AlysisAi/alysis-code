"""Product identity, and the compatibility shims that keep pre-rebrand installs working.

The CLI shipped as "Sylliptor" (package ``sylliptor-agent-cli``, command
``sylliptor``, env prefix ``SYLLIPTOR_``) before being renamed to Alysis Code.
Everything a user could have written down — env vars in a CI config, a
``.sylliptor/`` directory committed to their repo, a keyring entry holding MCP
OAuth tokens — still resolves. The fallbacks are deliberately *silent*: they
are a courtesy to people who already migrated once, not something to announce
above the TUI on every launch. Each one is recorded to the log at warning level
so a support session can still see which legacy input was used. See
``docs/migration-alysis-code.md`` for the removal timeline.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Mapping
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_config_dir, user_data_dir

LOGGER = logging.getLogger(__name__)

PRODUCT_NAME = "Alysis Code"
CANONICAL_APP_NAME = "alysis"
CANONICAL_SERVER_APP_NAME = "alysis-code-server"
PYTHON_PACKAGE_NAME = "alysis-code"
PROJECT_SOURCE_URL = "https://github.com/AlysisAi/alysis-code"
SANDBOX_IMAGE_REPOSITORY = "alysis-sandbox"
ENV_PREFIX = "ALYSIS_"
PROJECT_DIR_NAME = ".alysis"
PLUGIN_MANIFEST_NAME = "alysis-plugin.toml"

# --- pre-rebrand identities -------------------------------------------------
LEGACY_PRODUCT_NAME = "Sylliptor"
LEGACY_APP_NAME = "sylliptor"
LEGACY_SERVER_APP_NAME = "sylliptor-agent-cli-server"
LEGACY_PYTHON_PACKAGE_NAME = "sylliptor-agent-cli"
LEGACY_ENV_PREFIX = "SYLLIPTOR_"
LEGACY_PROJECT_DIR_NAME = ".sylliptor"
LEGACY_PLUGIN_MANIFEST_NAME = "sylliptor-plugin.toml"

# Written into a migrated legacy directory so a second run does not re-copy, and
# so a human poking around understands why the directory is now inert.
MIGRATION_MARKER_NAME = ".migrated-to-alysis"

_notice_lock = threading.Lock()
_legacy_env_seen: dict[str, str] = {}


def legacy_env_name(name: str) -> str | None:
    """Return the pre-rebrand spelling of an ``ALYSIS_*`` variable, if any."""
    if not name.startswith(ENV_PREFIX):
        return None
    return LEGACY_ENV_PREFIX + name[len(ENV_PREFIX) :]


def _record_legacy_env(legacy: str, current: str) -> None:
    """Log a legacy variable read, once per variable, without warning the user."""
    with _notice_lock:
        if legacy in _legacy_env_seen:
            return
        _legacy_env_seen[legacy] = current
    LOGGER.warning("legacy_env_var legacy=%s current=%s", legacy, current)


def env_get(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, falling back to its pre-rebrand name.

    The new name always wins when both are set, so a user midway through the
    migration gets the value they most recently wrote.
    """
    return env_get_from(os.environ, name, default)


def env_get_from(
    environ: Mapping[str, str],
    name: str,
    default: str | None = None,
) -> str | None:
    """Read ``name`` from a mapping with the same rename fallback as :func:`env_get`.

    Runtime settings often accept an injected environment mapping for tests or
    subprocess construction. Keeping the prefix-derived fallback here prevents
    those paths from drifting from process-environment behavior.
    """
    value = environ.get(name)
    if value is not None:
        return value

    legacy = legacy_env_name(name)
    if legacy is not None:
        legacy_value = environ.get(legacy)
        if legacy_value is not None:
            _record_legacy_env(legacy, name)
            return legacy_value

    return default


def with_legacy_env_aliases(env: Mapping[str, str]) -> dict[str, str]:
    """Duplicate ``ALYSIS_*`` entries under their legacy names.

    Used when building the environment for code we do not control — user-authored
    hooks and custom tools, which may still read ``SYLLIPTOR_TOOL_NAME`` and
    friends. Existing legacy keys in ``env`` are left alone.
    """
    merged = dict(env)
    for key, value in env.items():
        legacy = legacy_env_name(key)
        if legacy is not None and legacy not in merged:
            merged[legacy] = value
    return merged


def reset_legacy_tracking() -> None:
    """Forget which legacy inputs have been seen, so logging repeats.

    Only the per-process de-duplication state; nothing user-visible depends on
    it. Tests use it to isolate cases that each expect a fresh log line.
    """
    with _notice_lock:
        _legacy_env_seen.clear()


def _github_owner_from_url(url: str) -> str | None:
    value = url.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
        parts = [part for part in path.removesuffix(".git").split("/") if part]
        return parts[0] if len(parts) >= 2 else None

    parsed = urlparse(value)
    if parsed.hostname != "github.com":
        return None
    parts = [part for part in parsed.path.removesuffix(".git").strip("/").split("/") if part]
    return parts[0] if len(parts) >= 2 else None


def _packaging_source_urls() -> tuple[str, ...]:
    urls: list[str] = [PROJECT_SOURCE_URL]
    try:
        package_metadata = importlib_metadata.metadata(PYTHON_PACKAGE_NAME)
    except importlib_metadata.PackageNotFoundError:
        return tuple(urls)

    homepage = package_metadata.get("Home-page")
    if homepage:
        urls.append(homepage)
    for project_url in package_metadata.get_all("Project-URL") or ():
        label, _, value = project_url.partition(",")
        if label.strip().lower() in {"source", "repository", "homepage"} and value.strip():
            urls.append(value.strip())
    return tuple(dict.fromkeys(urls))


def resolve_ghcr_owner() -> str | None:
    for source_url in _packaging_source_urls():
        owner = _github_owner_from_url(source_url)
        if owner:
            return owner
    return None


def default_sandbox_docker_image(variant: str = "dev") -> str:
    tag = variant.strip() or "dev"
    owner = resolve_ghcr_owner()
    if owner:
        return f"ghcr.io/{owner.lower()}/{SANDBOX_IMAGE_REPOSITORY}:{tag}"
    return f"{SANDBOX_IMAGE_REPOSITORY}:{tag}"


def _migrate_legacy_dir(current: Path, legacy: Path) -> Path:
    """Seed ``current`` from a pre-rebrand ``legacy`` directory, once.

    The legacy tree is *copied*, not moved: an older install sharing the machine
    keeps working, and a failed copy never destroys the only copy of a user's
    credentials. A marker file in the legacy directory makes this idempotent.
    """
    if current.exists() or not legacy.is_dir():
        return current
    if (legacy / MIGRATION_MARKER_NAME).exists():
        return current

    try:
        shutil.copytree(legacy, current, dirs_exist_ok=False)
    except (OSError, shutil.Error) as exc:
        # A failed migration must not be fatal: fall through to a fresh
        # directory rather than blocking startup entirely.
        LOGGER.warning(
            "legacy_dir_migration_failed legacy=%s current=%s error=%s", legacy, current, exc
        )
        return current

    try:
        (legacy / MIGRATION_MARKER_NAME).write_text(
            f"Copied to {current} during the Sylliptor -> Alysis Code rename.\n"
            "This directory is no longer read. Delete it once you are confident "
            "the migration went through.\n",
            encoding="utf-8",
        )
    except OSError:
        # Marker is an optimisation; a missing one only costs a redundant check.
        pass

    LOGGER.info("legacy_dir_migrated legacy=%s current=%s", legacy, current)
    return current


def canonical_user_config_dir() -> Path:
    current = Path(user_config_dir(CANONICAL_APP_NAME, CANONICAL_APP_NAME))
    legacy = Path(user_config_dir(LEGACY_APP_NAME, LEGACY_APP_NAME))
    return _migrate_legacy_dir(current, legacy)


def canonical_user_data_dir() -> Path:
    current = Path(user_data_dir(CANONICAL_APP_NAME, CANONICAL_APP_NAME))
    legacy = Path(user_data_dir(LEGACY_APP_NAME, LEGACY_APP_NAME))
    return _migrate_legacy_dir(current, legacy)


def canonical_server_data_dir() -> Path:
    current = Path(user_data_dir(CANONICAL_SERVER_APP_NAME, CANONICAL_APP_NAME))
    legacy = Path(user_data_dir(LEGACY_SERVER_APP_NAME, LEGACY_APP_NAME))
    return _migrate_legacy_dir(current, legacy)


def resolve_project_dir(root: Path) -> Path:
    """Return the per-repo agent directory, preferring ``.alysis``.

    A ``.sylliptor`` directory is honoured in place when it is the only one
    present: it is typically committed to the user's repository, so silently
    renaming it would show up as an unexplained diff in their working tree.
    """
    current = root / PROJECT_DIR_NAME
    if current.exists():
        return current
    legacy = root / LEGACY_PROJECT_DIR_NAME
    if legacy.is_dir():
        LOGGER.debug("legacy_project_dir_in_use legacy=%s current=%s", legacy, current)
        return legacy
    return current


def plugin_manifest_candidates() -> tuple[str, ...]:
    """Manifest filenames to probe, newest spelling first."""
    return (PLUGIN_MANIFEST_NAME, LEGACY_PLUGIN_MANIFEST_NAME)
