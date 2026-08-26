from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..atomic_io import atomic_write_json
from .paths import workspace_trust_path as _workspace_trust_path

WORKSPACE_TRUST_SCHEMA_VERSION = 1


class WorkspaceTrustEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: str
    granted_at: str
    overrides_sha256: str


class WorkspaceTrustState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = WORKSPACE_TRUST_SCHEMA_VERSION
    trusted: dict[str, WorkspaceTrustEntry] = Field(default_factory=dict)


def workspace_trust_path(*, user_config_dir: Path | None = None) -> Path:
    return _workspace_trust_path(user_config_dir=user_config_dir)


def load_workspace_trust(*, user_config_dir: Path | None = None) -> WorkspaceTrustState:
    path = workspace_trust_path(user_config_dir=user_config_dir)
    if not path.exists():
        return WorkspaceTrustState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"Failed to read {path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid JSON format in {path}: expected object.")
    return WorkspaceTrustState.model_validate(raw)


def _canonical_workspace_root(repo_root: Path) -> str:
    resolved = repo_root.expanduser().resolve(strict=False)
    text = os.fspath(resolved)
    if os.name == "nt":
        drive, tail = os.path.splitdrive(text)
        text = drive.lower() + tail
    return text.rstrip("\\/") or text


def workspace_trust_key(repo_root: Path) -> str:
    """SHA-256 of the canonical absolute path."""
    canonical = _canonical_workspace_root(repo_root)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_workspace_trusted(
    *,
    repo_root: Path,
    overrides_sha256: str,
    user_config_dir: Path | None = None,
) -> bool:
    """Return True iff this repo and overrides-file hash were explicitly trusted."""
    state = load_workspace_trust(user_config_dir=user_config_dir)
    entry = state.trusted.get(workspace_trust_key(repo_root))
    return entry is not None and entry.overrides_sha256 == overrides_sha256


def grant_workspace_trust(
    *,
    repo_root: Path,
    overrides_sha256: str,
    user_config_dir: Path | None = None,
) -> None:
    """Write a workspace trust entry atomically."""
    state = load_workspace_trust(user_config_dir=user_config_dir)
    key = workspace_trust_key(repo_root)
    updated = WorkspaceTrustState(
        schema_version=WORKSPACE_TRUST_SCHEMA_VERSION,
        trusted={
            **state.trusted,
            key: WorkspaceTrustEntry(
                workspace_root=_canonical_workspace_root(repo_root),
                granted_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                overrides_sha256=overrides_sha256,
            ),
        },
    )
    atomic_write_json(
        workspace_trust_path(user_config_dir=user_config_dir),
        updated.model_dump(mode="json"),
    )
