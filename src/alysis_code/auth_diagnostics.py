"""Machine-readable auth diagnostics for supervising applications.

A GUI that spawns the CLI cannot infer auth state from human console text, and a
spawned process does not necessarily see the same environment — most notably the
OS keyring — as the interactive shell the user logged in from. Everything here is
non-secret and safe to hand to a supervising app.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .branding import env_get
from .config import config_path
from .mcp.token_store import (
    KeyringOutcome,
    TokenPayloadLoadResult,
    fallback_key_source,
    keyring_availability,
    keyring_backend_name,
    last_keyring_outcome,
    load_token_payload_result,
    planned_key_source,
    stored_key_source,
)

CONTEXT_INTERACTIVE = "interactive"
CONTEXT_SPAWNED = "spawned"

# A GUI-spawned process on macOS typically inherits only the system default
# PATH, which is how a CLI that works in a terminal can fail when spawned.
_MINIMAL_PATH_ENTRY_COUNT = 4
_CI_ENV_KEYS = ("CI", "GITHUB_ACTIONS", "BUILDKITE", "TEAMCITY_VERSION", "JENKINS_URL")


@dataclass(frozen=True, slots=True)
class CredentialStoreHealth:
    """Non-secret health of one encrypted credential store."""

    name: str
    path: str
    exists: bool
    readable: bool
    key_source: str | None = None
    planned_key_source: str | None = None
    entry_count: int | None = None
    legacy_plaintext: bool = False
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "readable": self.readable,
            "key_source": self.key_source,
            "planned_key_source": self.planned_key_source,
            "entry_count": self.entry_count,
            "legacy_plaintext": self.legacy_plaintext,
            "error": self.error,
        }


def provider_token_store_path() -> Path:
    """Return the native provider credential vault path."""

    # Imported lazily: provider_auth.store imports the MCP token store, and a
    # module-level import here would make auth diagnostics depend on the whole
    # provider auth package just to name a file.
    from .provider_auth.store import provider_token_store_path as _path

    return _path()


def mcp_token_store_path() -> Path:
    """Return the MCP OAuth credential store path."""

    from .mcp.oauth_store import mcp_oauth_token_store_path

    return mcp_oauth_token_store_path()


def keyring_snapshot() -> dict[str, Any]:
    """Probe the OS keyring and describe what a credential write would use."""

    probe = keyring_availability()
    observed = last_keyring_outcome()
    return {
        "available": probe.available,
        "backend": probe.backend,
        "env_override": os.environ.get("PYTHON_KEYRING_BACKEND") or None,
        "reason": probe.reason,
        "error_type": probe.error_type,
        "detail": probe.detail,
        "observed_this_process": None if observed is None else _outcome_payload(observed),
    }


def _outcome_payload(outcome: KeyringOutcome) -> dict[str, Any]:
    return {
        "available": outcome.available,
        "reason": outcome.reason,
        "error_type": outcome.error_type,
        "backend": outcome.backend,
        "detail": outcome.detail,
    }


def credential_backend_fields() -> dict[str, Any]:
    """Return the keyring fields every auth status payload carries.

    A supervising app needs these to explain why auth state differs between the
    user's terminal and the process it spawned, instead of guessing.

    A degradation actually hit while serving this command is ground truth and wins
    over the backend classification. Absent that, the classification is used
    without a read probe, so reporting auth state never risks prompting the user
    to unlock a keyring.
    """

    observed = last_keyring_outcome()
    if observed is not None and not observed.available:
        probe = observed
    else:
        probe = keyring_availability(probe_read=False)
    return {
        "keyring_available": probe.available,
        "keyring_backend": probe.backend or keyring_backend_name(),
        "credential_fallback": None if probe.available else fallback_key_source(),
    }


def credential_store_health(name: str, path: Path) -> CredentialStoreHealth:
    """Report whether one encrypted credential store can actually be read."""

    planned = planned_key_source()
    if not path.exists():
        return CredentialStoreHealth(
            name=name,
            path=str(path),
            exists=False,
            readable=True,
            planned_key_source=planned,
            entry_count=0,
        )
    stored = stored_key_source(path)
    try:
        result: TokenPayloadLoadResult = load_token_payload_result(path)
    except Exception as exc:  # noqa: BLE001 - store backends fail in OS-specific ways
        return CredentialStoreHealth(
            name=name,
            path=str(path),
            exists=True,
            readable=False,
            key_source=stored,
            planned_key_source=planned,
            error=f"{exc.__class__.__name__}: {exc}",
        )
    return CredentialStoreHealth(
        name=name,
        path=str(path),
        exists=True,
        readable=True,
        key_source=stored,
        planned_key_source=planned,
        entry_count=len(result.payload),
        legacy_plaintext=result.legacy_plaintext,
    )


def credential_store_healths() -> tuple[CredentialStoreHealth, ...]:
    """Report health for every encrypted credential store auth depends on."""

    return (
        credential_store_health("provider_auth", provider_token_store_path()),
        credential_store_health("mcp_oauth", mcp_token_store_path()),
    )


def _isatty(stream_name: str) -> bool:
    stream = getattr(sys, stream_name, None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - replaced streams need not implement isatty
        return False


def context_snapshot() -> dict[str, Any]:
    """Describe whether this process looks interactive or spawned by another app."""

    stdin_tty = _isatty("stdin")
    stdout_tty = _isatty("stdout")
    stderr_tty = _isatty("stderr")
    ci = sorted(key for key in _CI_ENV_KEYS if os.environ.get(key))
    kind = CONTEXT_INTERACTIVE if stdin_tty and stdout_tty else CONTEXT_SPAWNED
    return {
        "kind": kind,
        "stdin_tty": stdin_tty,
        "stdout_tty": stdout_tty,
        "stderr_tty": stderr_tty,
        "term": os.environ.get("TERM") or None,
        "ci_env": ci,
        "ssh_session": bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION")),
    }


def _path_entries() -> list[str]:
    raw = os.environ.get("PATH") or ""
    return [entry for entry in raw.split(os.pathsep) if entry]


def path_snapshot() -> dict[str, Any]:
    """Describe where the CLI was resolved from and whether PATH looks truncated."""

    entries = _path_entries()
    return {
        "entry_count": len(entries),
        "looks_minimal": len(entries) <= _MINIMAL_PATH_ENTRY_COUNT,
        "resolved_cli": shutil.which("alysis"),
        "argv0": (sys.argv[0] or None) if sys.argv else None,
        "python_executable": sys.executable or None,
    }


def environment_snapshot() -> dict[str, Any]:
    """Report the auth-relevant environment a supervising app cannot see."""

    return {
        "home": os.path.expanduser("~"),
        "home_env": os.environ.get("HOME") or os.environ.get("USERPROFILE") or None,
        "config_dir": str(config_path().parent),
        "config_dir_override": env_get("ALYSIS_CONFIG_DIR") or None,
        "config_path": str(config_path()),
        "config_exists": config_path().exists(),
        "platform": sys.platform,
        "path": path_snapshot(),
    }


def auth_doctor_payload() -> dict[str, Any]:
    """Build the full ``alysis doctor auth --json`` report."""

    return {
        "context": context_snapshot(),
        "environment": environment_snapshot(),
        "keyring": keyring_snapshot(),
        "credential_stores": [health.to_payload() for health in credential_store_healths()],
    }


__all__ = [
    "CONTEXT_INTERACTIVE",
    "CONTEXT_SPAWNED",
    "CredentialStoreHealth",
    "auth_doctor_payload",
    "context_snapshot",
    "credential_backend_fields",
    "credential_store_health",
    "credential_store_healths",
    "environment_snapshot",
    "keyring_snapshot",
    "mcp_token_store_path",
    "path_snapshot",
    "provider_token_store_path",
]
