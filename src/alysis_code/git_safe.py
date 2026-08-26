from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .branding import env_get_from

_TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
_NON_INTERACTIVE_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
    "GIT_EDITOR": "true",
    "GIT_MERGE_AUTOEDIT": "no",
    "PAGER": "cat",
}


def git_hooks_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(env_get_from(source, "ALYSIS_GIT_HOOKS") or "").strip().lower()
    return raw in _TRUE_VALUES


def resolve_disabled_hooks_dir(root: Path, env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    override = str(env_get_from(source, "ALYSIS_GIT_HOOKS_PATH") or "").strip()
    if override:
        target = Path(override).expanduser()
    else:
        root_hash = hashlib.sha256(os.fspath(root.resolve()).encode("utf-8")).hexdigest()[:16]
        target = Path(tempfile.gettempdir()) / "alysis-agent" / "hooks-disabled" / root_hash
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_git_process_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    source.update(_NON_INTERACTIVE_GIT_ENV)
    return source


def build_git_cmd(
    root: Path,
    args: list[str],
    *,
    extra_config: dict[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    config = dict(extra_config or {})
    if not git_hooks_enabled(env):
        config.setdefault("core.hooksPath", os.fspath(resolve_disabled_hooks_dir(root, env)))

    cmd: list[str] = ["git", "-C", os.fspath(root)]
    for key, value in config.items():
        cmd.extend(["-c", f"{key}={value}"])
    cmd.extend(args)
    return cmd
