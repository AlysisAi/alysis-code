from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from io import BufferedRandom
from pathlib import Path

from ..atomic_io import atomic_write_json
from ..branding import canonical_user_config_dir, env_get
from .discovery import CustomToolSpec

_TRUST_SCHEMA_VERSION = 1
_TRUST_STATE_LOCK = threading.RLock()

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - exercised on POSIX
    import fcntl


@dataclass(frozen=True)
class ProjectToolTrustKey:
    workspace_root: str
    relative_tool_path: str
    file_hash: str


@dataclass(frozen=True)
class ProjectToolTrustState:
    trusted_tools: tuple[ProjectToolTrustKey, ...] = ()

    def contains(self, key: ProjectToolTrustKey) -> bool:
        return key in set(self.trusted_tools)


def _config_dir() -> Path:
    override = env_get("ALYSIS_CONFIG_DIR")
    if override:
        return Path(override)
    return canonical_user_config_dir()


def trust_state_path(*, user_config_dir: Path | None = None) -> Path:
    base = user_config_dir.expanduser().resolve() if user_config_dir is not None else _config_dir()
    return base / "custom_tools_trust.json"


def load_trust_state(*, user_config_dir: Path | None = None) -> ProjectToolTrustState:
    path = trust_state_path(user_config_dir=user_config_dir)
    return _load_trust_state_from_path(path)


def _load_trust_state_from_path(path: Path) -> ProjectToolTrustState:
    if not path.exists():
        return ProjectToolTrustState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to read custom tool trust state: {path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid custom tool trust state format.")
    version = raw.get("schema_version", _TRUST_SCHEMA_VERSION)
    if version != _TRUST_SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported custom tool trust schema version: {version}")
    trusted_raw = raw.get("trusted_tools", [])
    if not isinstance(trusted_raw, list):
        raise RuntimeError("Invalid custom tool trust state: trusted_tools must be an array.")
    trusted: list[ProjectToolTrustKey] = []
    for item in trusted_raw:
        if not isinstance(item, dict):
            continue
        workspace_root = str(item.get("workspace_root") or "").strip()
        relative_tool_path = str(item.get("relative_tool_path") or "").strip()
        file_hash = str(item.get("file_hash") or "").strip()
        if workspace_root and relative_tool_path and file_hash:
            trusted.append(
                ProjectToolTrustKey(
                    workspace_root=workspace_root,
                    relative_tool_path=relative_tool_path,
                    file_hash=file_hash,
                )
            )
    trusted_sorted = tuple(
        sorted(
            set(trusted),
            key=lambda key: (key.workspace_root, key.relative_tool_path, key.file_hash),
        )
    )
    return ProjectToolTrustState(trusted_tools=trusted_sorted)


def save_trust_state(
    state: ProjectToolTrustState,
    *,
    user_config_dir: Path | None = None,
) -> None:
    path = trust_state_path(user_config_dir=user_config_dir)
    with _locked_trust_state(path):
        _save_trust_state_to_path(state, path)


def _save_trust_state_to_path(
    state: ProjectToolTrustState,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _TRUST_SCHEMA_VERSION,
        "trusted_tools": [
            {
                "workspace_root": key.workspace_root,
                "relative_tool_path": key.relative_tool_path,
                "file_hash": key.file_hash,
            }
            for key in state.trusted_tools
        ],
    }
    atomic_write_json(path, payload, ensure_ascii=True)


def trust_project_tool(
    spec: CustomToolSpec,
    *,
    user_config_dir: Path | None = None,
) -> ProjectToolTrustState:
    key = project_tool_trust_key(spec)
    return _mutate_trust_state(
        user_config_dir=user_config_dir,
        mutation=lambda current: ProjectToolTrustState(
            trusted_tools=tuple(
                sorted(
                    {*(current.trusted_tools), key},
                    key=lambda item: (item.workspace_root, item.relative_tool_path, item.file_hash),
                )
            )
        ),
    )


def untrust_project_tool(
    spec: CustomToolSpec,
    *,
    user_config_dir: Path | None = None,
) -> ProjectToolTrustState:
    if spec.source_scope != "project":
        raise ValueError("Only project custom tools can be untrusted.")
    workspace_root = os.fspath(spec.source_path.resolve().parents[2])
    relpath = spec.relative_tool_path
    return _mutate_trust_state(
        user_config_dir=user_config_dir,
        mutation=lambda current: ProjectToolTrustState(
            trusted_tools=tuple(
                key
                for key in current.trusted_tools
                if not (key.workspace_root == workspace_root and key.relative_tool_path == relpath)
            )
        ),
    )


def is_project_tool_trusted(
    spec: CustomToolSpec,
    *,
    state: ProjectToolTrustState | None = None,
    user_config_dir: Path | None = None,
) -> bool:
    if spec.source_scope != "project":
        return True
    active_state = state if state is not None else load_trust_state(user_config_dir=user_config_dir)
    return project_tool_trust_key(spec) in set(active_state.trusted_tools)


def project_tool_trust_key(spec: CustomToolSpec) -> ProjectToolTrustKey:
    if spec.source_scope != "project":
        raise ValueError("Only project custom tools use explicit trust keys.")
    workspace_root = os.fspath(spec.source_path.resolve().parents[2])
    return ProjectToolTrustKey(
        workspace_root=workspace_root,
        relative_tool_path=spec.relative_tool_path,
        file_hash=spec.file_hash,
    )


def _mutate_trust_state(
    *,
    user_config_dir: Path | None,
    mutation: Callable[[ProjectToolTrustState], ProjectToolTrustState],
) -> ProjectToolTrustState:
    path = trust_state_path(user_config_dir=user_config_dir)
    with _locked_trust_state(path):
        current = _load_trust_state_from_path(path)
        updated = mutation(current)
        if updated != current:
            _save_trust_state_to_path(updated, path)
        return updated


@contextmanager
def _locked_trust_state(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with _TRUST_STATE_LOCK:
        with lock_path.open("a+b") as handle:
            _acquire_file_lock(handle)
            try:
                yield
            finally:
                _release_file_lock(handle)


def _acquire_file_lock(handle: BufferedRandom) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle: BufferedRandom) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
