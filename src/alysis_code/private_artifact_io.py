"""Private atomic artifact publication without runtime or model imports."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
    except OSError:
        return True


def _has_safe_directory_ancestors(*, path: Path, root: Path) -> bool:
    """Require every existing descendant of root to be a real contained directory."""

    root_abs = root.resolve()
    try:
        relative = path.absolute().relative_to(root_abs)
    except (OSError, RuntimeError, ValueError):
        return False
    current = root_abs
    for component in relative.parts:
        current = current / component
        try:
            exists = current.exists()
        except OSError:
            return False
        if not exists:
            continue
        if _is_link_like(current) or not current.is_dir():
            return False
        try:
            current.resolve().relative_to(root_abs)
        except (OSError, RuntimeError, ValueError):
            return False
    return True


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # Windows ACLs are not represented completely by POSIX mode bits.
        pass


def _atomic_private_write_text(
    path: Path,
    content: str,
    *,
    containment_root: Path | None = None,
) -> None:
    """Publish one complete private artifact or leave the old path untouched."""

    if containment_root is not None and not _has_safe_directory_ancestors(
        path=path.parent,
        root=containment_root,
    ):
        raise OSError("tool output artifact directory is not safely contained")
    _ensure_private_directory(path.parent)
    if containment_root is not None and not _has_safe_directory_ancestors(
        path=path.parent,
        root=containment_root,
    ):
        raise OSError("tool output artifact directory is not safely contained")
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        # A directory fsync makes the rename durable on filesystems that support it.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
