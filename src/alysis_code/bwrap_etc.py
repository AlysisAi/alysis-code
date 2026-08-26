from __future__ import annotations

import os
import tempfile
from pathlib import Path

_VALID_NETWORK = {"off", "on"}


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except OSError:
            pass
    path.write_text(content, encoding="utf-8")


def _resolve_uid_gid() -> tuple[int, int]:
    uid = 0
    gid = 0
    if hasattr(os, "getuid"):
        try:
            uid = int(os.getuid())
        except OSError:
            uid = 0
    if hasattr(os, "getgid"):
        try:
            gid = int(os.getgid())
        except OSError:
            gid = 0
    return uid, gid


def ensure_minimal_etc_dir(*, network: str) -> Path:
    mode = str(network).strip().lower()
    if mode not in _VALID_NETWORK:
        raise ValueError(f"Invalid network mode for minimal /etc: {network!r}")

    root = Path(tempfile.gettempdir()) / "alysis-code" / "bwrap-etc" / mode
    root.mkdir(parents=True, exist_ok=True)

    uid, gid = _resolve_uid_gid()
    passwd_lines = ["root:x:0:0:root:/tmp/home:/bin/sh"]
    if uid != 0:
        passwd_lines.append(f"user:x:{uid}:{gid}:user:/tmp/home:/bin/sh")
    _write_if_changed(root / "passwd", "\n".join(passwd_lines) + "\n")

    group_lines = ["root:x:0:"]
    if gid != 0:
        group_lines.append(f"user:x:{gid}:")
    _write_if_changed(root / "group", "\n".join(group_lines) + "\n")

    _write_if_changed(
        root / "nsswitch.conf",
        "passwd: files\ngroup: files\nhosts: files dns\n",
    )
    _write_if_changed(root / "hosts", "127.0.0.1 localhost\n::1 localhost\n")

    resolv_path = root / "resolv.conf"
    if mode == "on":
        host_resolv = Path("/etc/resolv.conf")
        try:
            if host_resolv.exists():
                _write_if_changed(resolv_path, host_resolv.read_text(encoding="utf-8"))
            else:
                _write_if_changed(resolv_path, "")
        except OSError:
            _write_if_changed(resolv_path, "")
    else:
        _write_if_changed(resolv_path, "")

    (root / "ssl").mkdir(parents=True, exist_ok=True)
    (root / "ssl" / "certs").mkdir(parents=True, exist_ok=True)
    return root
