from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from .state import SkillLifecycleError


def commit_managed_bundle_update(
    *,
    target_path: Path,
    force: bool,
    stage_bundle: Callable[[Path], None],
    persist_state: Callable[[], None],
) -> None:
    existing_target = target_path.exists() or target_path.is_symlink()
    if existing_target and not force:
        raise SkillLifecycleError(f"Managed skill already exists: {target_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    installed_target = False
    with tempfile.TemporaryDirectory(
        prefix=f".{target_path.name}.stage-",
        dir=target_path.parent,
    ) as stage_dir:
        staged_path = Path(stage_dir) / target_path.name
        stage_bundle(staged_path)
        if not (staged_path.exists() or staged_path.is_symlink()):
            raise SkillLifecycleError(f"Staged managed skill bundle was not created: {staged_path}")
        try:
            if existing_target:
                backup_path = _backup_path_for(target_path)
                os.replace(target_path, backup_path)
            os.replace(staged_path, target_path)
            installed_target = True
            persist_state()
        except Exception as exc:  # noqa: BLE001
            _rollback_managed_bundle_update(
                target_path=target_path,
                backup_path=backup_path,
                installed_target=installed_target,
                cause=exc,
            )
            raise
        else:
            if backup_path is not None:
                _best_effort_remove(backup_path)


def commit_managed_bundle_removal(
    *,
    target_path: Path,
    persist_state: Callable[[], None],
) -> None:
    if not (target_path.exists() or target_path.is_symlink()):
        persist_state()
        return

    backup_path = _backup_path_for(target_path)
    try:
        os.replace(target_path, backup_path)
        persist_state()
    except Exception as exc:  # noqa: BLE001
        rollback_errors: list[str] = []
        if backup_path.exists() or backup_path.is_symlink():
            try:
                os.replace(backup_path, target_path)
            except Exception as restore_exc:  # noqa: BLE001
                rollback_errors.append(f"failed to restore backup: {restore_exc}")
        if rollback_errors:
            raise SkillLifecycleError(
                "Managed skill remove failed and rollback also failed: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    else:
        _best_effort_remove(backup_path)


def _rollback_managed_bundle_update(
    *,
    target_path: Path,
    backup_path: Path | None,
    installed_target: bool,
    cause: Exception,
) -> None:
    rollback_errors: list[str] = []
    if installed_target and (target_path.exists() or target_path.is_symlink()):
        try:
            _remove_path(target_path)
        except Exception as cleanup_exc:  # noqa: BLE001
            rollback_errors.append(f"failed to remove staged bundle: {cleanup_exc}")
    if backup_path is not None and (backup_path.exists() or backup_path.is_symlink()):
        try:
            os.replace(backup_path, target_path)
        except Exception as restore_exc:  # noqa: BLE001
            rollback_errors.append(f"failed to restore previous bundle: {restore_exc}")
    if rollback_errors:
        raise SkillLifecycleError(
            "Managed skill update failed and rollback also failed: " + "; ".join(rollback_errors)
        ) from cause


def _backup_path_for(target_path: Path) -> Path:
    return target_path.parent / f".{target_path.name}.backup-{uuid4().hex}"


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def _best_effort_remove(path: Path) -> None:
    with suppress(Exception):  # noqa: BLE001
        _remove_path(path)
