"""Open URLs in the user's graphical browser across native and WSL hosts."""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

_LAUNCH_TIMEOUT_SECONDS = 8.0


def is_wsl() -> bool:
    """Return whether the current Python process is running inside WSL."""

    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def open_url(url: str) -> bool:
    """Open *url* in the host browser, including the Windows browser from WSL."""

    target = str(url or "").strip()
    if not target:
        return False
    if is_wsl() and _open_url_from_wsl(target):
        return True
    try:
        return bool(webbrowser.open(target))
    except Exception:  # noqa: BLE001 - browser launch is best-effort
        return False


def _open_url_from_wsl(url: str) -> bool:
    for command in _wsl_browser_commands(url):
        try:
            result = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_LAUNCH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


def _wsl_browser_commands(url: str) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = []
    wslview = shutil.which("wslview")
    if wslview:
        commands.append((wslview, url))

    powershell = _windows_executable(
        "powershell.exe",
        Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
    )
    if powershell:
        commands.append(
            (
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Start-Process -FilePath $args[0]",
                url,
            )
        )

    protocol_handler = _windows_executable(
        "rundll32.exe",
        Path("/mnt/c/Windows/System32/rundll32.exe"),
    )
    if protocol_handler:
        commands.append((protocol_handler, "url.dll,FileProtocolHandler", url))
    return tuple(commands)


def _windows_executable(name: str, fallback: Path) -> str | None:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    try:
        if fallback.is_file():
            return os.fspath(fallback)
    except OSError:
        pass
    return None


__all__ = ["is_wsl", "open_url"]
