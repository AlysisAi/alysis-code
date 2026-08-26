#!/usr/bin/env python3
"""PreToolUse hook that blocks access to sensitive config paths.

Applies to fs_read, fs_write, fs_patch, and fs_edit.
Blocks .env files, credentials, private keys, key archives, and secrets/.
Allows .env.example, .env.sample, .env.template, and .env.dist.
Allow repo-relative paths with ALYSIS_HOOK_ALLOW_PATHS.
Example .alysis/hooks.json:
    {"hooks": {"PreToolUse": [{"matcher": "fs_read|fs_write|fs_patch|fs_edit",
      "hooks": [{"type": "command", "id": "security.block-env-files",
      "command": "python docs/examples/hooks/block_env_files.py",
      "failurePolicy": "warn"}]}]}}
Only block decisions are written to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# fmt: off
BLOCKED_NAMES = [".env", ".env.local", ".env.production", ".env.development", "credentials.json", "credentials.yml", "credentials.yaml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"]
# fmt: on
BLOCKED_GLOBS = [".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "secrets/**", "**/secrets/*"]
ALLOWED_ENV_SUFFIXES = [".example", ".sample", ".template", ".dist"]


def _read_payload() -> dict:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def _requested_path(payload: dict) -> Path | None:
    if payload.get("tool_name") not in {"fs_read", "fs_write", "fs_patch", "fs_edit"}:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw_path = tool_input.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    cwd = payload.get("cwd")
    base = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
    return base / path


def _repo_rel(path: Path, cwd: Path) -> str:
    try:
        return _normalize(os.path.relpath(os.fspath(path), os.fspath(cwd)))
    except ValueError:
        return _normalize(os.fspath(path))


def _allow_paths() -> set[str]:
    raw = os.environ.get("ALYSIS_HOOK_ALLOW_PATHS", "")
    return {_normalize(value).lower() for value in raw.split(os.pathsep) if value.strip()}


def _path_blocked(relpath: str) -> bool:
    rel = _normalize(relpath).lower()
    parts = [part.lower() for part in rel.split("/") if part]
    name = parts[-1] if parts else ""
    if name.startswith(".env.") and any(name.endswith(suffix) for suffix in ALLOWED_ENV_SUFFIXES):
        return False
    if name in {blocked.lower() for blocked in BLOCKED_NAMES}:
        return True
    if "secrets" in parts[:-1]:
        return True
    if any(Path(rel).match(glob) for glob in BLOCKED_GLOBS):
        return True
    return re.search(r"\.(pem|key|p12|pfx)$", name) is not None


def main() -> int:
    payload = _read_payload()
    path = _requested_path(payload)
    if path is None:
        return 0
    cwd = Path(payload.get("cwd")) if isinstance(payload.get("cwd"), str) else Path.cwd()
    requested_rel = _repo_rel(path, cwd)
    resolved_rel = _repo_rel(path.resolve(strict=False), cwd)
    allowed = _allow_paths()
    if requested_rel.lower() in allowed or resolved_rel.lower() in allowed:
        return 0
    relpath = requested_rel if _path_blocked(requested_rel) else resolved_rel
    if _path_blocked(relpath):
        reason = f"Access denied to sensitive path: {relpath}"
        print(json.dumps({"decision": "block", "reason": reason}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
