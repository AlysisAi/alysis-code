"""Terminal background theme detection shared by Rich and owl renderers."""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import sys
import time
from pathlib import Path
from typing import Any

from ..branding import env_get
from .styles import TerminalTheme


def _stream_is_tty(stream: Any | None) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def _theme_debug(message: str) -> None:
    if env_get("ALYSIS_THEME_DEBUG"):
        print(f"[alysis-theme] {message}", file=sys.stderr)


def _truthy_env(name: str) -> bool:
    return str(env_get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_wsl() -> bool:
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _normalize_theme(value: str | None) -> TerminalTheme | None:
    raw = str(value or "").strip().lower()
    if raw in {"light", "dark", "neutral"}:
        return raw  # type: ignore[return-value]
    return None


def _theme_from_colorfgbg(value: str | None) -> TerminalTheme | None:
    if not value:
        return None
    bg = value.rsplit(";", 1)[-1].strip()
    if not bg.isdigit():
        return None
    bg_code = int(bg)
    return "dark" if bg_code <= 6 or bg_code == 8 else "light"


def _hex_to_8bit(value: str) -> int:
    max_value = (16 ** len(value)) - 1
    if max_value <= 0:
        return 0
    return int(value, 16) * 255 // max_value


def _theme_from_osc11(stream: Any | None) -> TerminalTheme | None:
    # OSC 11 is unreliable through Windows ConPTY/WSL: replies can arrive
    # after our timeout and leak into the next /dev/tty reader.
    if _is_wsl():
        _theme_debug("OSC11=skipped (WSL)")
        return None
    if not _stream_is_tty(stream):
        _theme_debug("OSC11=skipped (stream is not a TTY)")
        return None
    try:
        import termios
        import tty

        tty_fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except Exception:
        return None
    old_attrs = None
    response = ""
    try:
        old_attrs = termios.tcgetattr(tty_fd)
        tty.setraw(tty_fd, termios.TCSANOW)
        os.write(tty_fd, b"\033]11;?\007")
        ready, _, _ = select.select([tty_fd], [], [], 0.5)
        if not ready:
            _theme_debug("OSC11=timeout -> none")
            return None
        response = os.read(tty_fd, 128).decode("ascii", errors="ignore")
    except Exception:
        return None
    finally:
        # Best-effort drain: consume trailing terminal response bytes before
        # prompt_toolkit or another /dev/tty reader can see them as input.
        with contextlib.suppress(Exception):
            os.set_blocking(tty_fd, False)
            deadline = time.monotonic() + 0.025
            while True:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    break
                ready, _, _ = select.select([tty_fd], [], [], min(timeout, 0.005))
                if not ready:
                    continue
                if not os.read(tty_fd, 4096):
                    break
        if old_attrs is not None:
            with contextlib.suppress(Exception):
                termios.tcsetattr(tty_fd, termios.TCSANOW, old_attrs)
        with contextlib.suppress(Exception):
            os.close(tty_fd)

    match = re.search(r"rgb:([0-9A-Fa-f]+)/([0-9A-Fa-f]+)/([0-9A-Fa-f]+)", response)
    if not match:
        _theme_debug(f"OSC11={response!r} -> none")
        return None
    red = _hex_to_8bit(match.group(1))
    green = _hex_to_8bit(match.group(2))
    blue = _hex_to_8bit(match.group(3))
    brightness = (red * 299 + green * 587 + blue * 114) // 1000
    detected: TerminalTheme = "light" if brightness >= 128 else "dark"
    _theme_debug(f"OSC11=rgb:{match.group(1)}/{match.group(2)}/{match.group(3)} -> {detected}")
    return detected


def _strip_json_comments(value: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    in_line_comment = False
    in_block_comment = False
    index = 0
    while index < len(value):
        char = value[index]
        next_char = value[index + 1] if index + 1 < len(value) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                output.append(char)
            index += 1
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue
        output.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(output))


def _safe_is_dir(path: Path) -> bool:
    with contextlib.suppress(OSError):
        return path.is_dir()
    return False


def _safe_is_file(path: Path) -> bool:
    with contextlib.suppress(OSError):
        return path.is_file()
    return False


def _safe_sorted_glob(path: Path, pattern: str) -> list[Path]:
    with contextlib.suppress(OSError):
        return sorted(path.glob(pattern))
    return []


def _safe_sorted_iterdir(path: Path) -> list[Path]:
    with contextlib.suppress(OSError):
        return sorted(path.iterdir())
    return []


def _windows_terminal_settings_paths() -> list[Path]:
    override = env_get("ALYSIS_WINDOWS_TERMINAL_SETTINGS")
    if override:
        return [Path(override).expanduser()]

    paths: list[Path] = []
    drive_roots = [
        *_safe_sorted_glob(Path("/mnt"), "[a-z]"),
        *_safe_sorted_glob(Path("/mnt/host"), "[a-z]"),
    ]
    for drive_root in dict.fromkeys(drive_roots):
        users_dir = drive_root / "Users"
        if not _safe_is_dir(users_dir):
            continue
        for user_dir in _safe_sorted_iterdir(users_dir):
            local_app_data = user_dir / "AppData" / "Local"
            packages_dir = local_app_data / "Packages"
            paths.extend(
                _safe_sorted_glob(
                    packages_dir,
                    "Microsoft.WindowsTerminal*_8wekyb3d8bbwe/LocalState/settings.json",
                )
            )
            paths.append(local_app_data / "Microsoft" / "Windows Terminal" / "settings.json")
    return paths


def _load_jsonc_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_strip_json_comments(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _theme_from_hex_background(value: Any) -> TerminalTheme | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    if len(raw) != 6 or re.search(r"[^0-9A-Fa-f]", raw):
        return None
    red = int(raw[0:2], 16)
    green = int(raw[2:4], 16)
    blue = int(raw[4:6], 16)
    brightness = (red * 299 + green * 587 + blue * 114) // 1000
    return "light" if brightness >= 128 else "dark"


def _windows_terminal_builtin_scheme_theme(name: str) -> TerminalTheme | None:
    normalized = re.sub(r"[^a-z0-9+]+", " ", name.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    known: dict[str, TerminalTheme] = {
        "atom one dark": "dark",
        "atom one light": "light",
        "ayu dark": "dark",
        "ayu light": "light",
        "ayu mirage": "dark",
        "campbell": "dark",
        "campbell powershell": "dark",
        "dark+": "dark",
        "dracula": "dark",
        "dracula+": "dark",
        "github dark": "dark",
        "github light": "light",
        "gruvbox dark": "dark",
        "gruvbox light": "light",
        "light+": "light",
        "material dark": "dark",
        "material light": "light",
        "monokai": "dark",
        "monokai pro": "dark",
        "nord": "dark",
        "one half dark": "dark",
        "one half light": "light",
        "powershell": "dark",
        "solarized dark": "dark",
        "solarized light": "light",
        "tango dark": "dark",
        "tango light": "light",
        "tokyo night": "dark",
        "tokyo night light": "light",
        "tokyo night storm": "dark",
        "ubuntu": "dark",
        "vintage": "dark",
    }
    return known.get(normalized)


def _windows_terminal_scheme_name(settings: dict[str, Any]) -> str | None:
    profiles = settings.get("profiles")
    profiles = profiles if isinstance(profiles, dict) else {}
    profile_list = profiles.get("list")
    profile_list = profile_list if isinstance(profile_list, list) else []
    profile_defaults = profiles.get("defaults")
    profile_defaults = profile_defaults if isinstance(profile_defaults, dict) else {}
    profile_id = str(os.environ.get("WT_PROFILE_ID") or "").strip().strip("{}").lower()

    profile: dict[str, Any] | None = None
    if profile_id:
        for candidate in profile_list:
            if not isinstance(candidate, dict):
                continue
            guid = str(candidate.get("guid") or "").strip().strip("{}").lower()
            if guid == profile_id:
                profile = candidate
                break
    if profile is None:
        default_profile = str(settings.get("defaultProfile") or "").strip().strip("{}").lower()
        for candidate in profile_list:
            if not isinstance(candidate, dict):
                continue
            guid = str(candidate.get("guid") or "").strip().strip("{}").lower()
            if guid == default_profile:
                profile = candidate
                break

    scheme = (profile or {}).get("colorScheme")
    if isinstance(scheme, str) and scheme.strip():
        return scheme.strip()
    scheme = profile_defaults.get("colorScheme")
    if isinstance(scheme, str) and scheme.strip():
        return scheme.strip()
    return "Campbell"


def _theme_from_windows_terminal_settings() -> TerminalTheme | None:
    has_windows_terminal = bool(os.environ.get("WT_SESSION"))
    override = env_get("ALYSIS_WINDOWS_TERMINAL_SETTINGS")
    is_wsl = _is_wsl()
    _theme_debug(f"is_wsl={is_wsl}, WT_SESSION={'present' if has_windows_terminal else 'missing'}")
    if not has_windows_terminal and not override and not is_wsl:
        return None
    try:
        settings_paths = _windows_terminal_settings_paths()
    except OSError as exc:
        _theme_debug(f"settings.json paths failed: {exc}")
        return "dark" if has_windows_terminal else None
    _theme_debug(f"settings.json paths tried: {len(settings_paths)}")
    for settings_path in settings_paths:
        if not _safe_is_file(settings_path):
            continue
        settings = _load_jsonc_file(settings_path)
        if not settings:
            continue
        scheme_name = _windows_terminal_scheme_name(settings)
        if not scheme_name:
            continue
        schemes = settings.get("schemes")
        schemes = schemes if isinstance(schemes, list) else []
        for scheme in schemes:
            if not isinstance(scheme, dict):
                continue
            name = str(scheme.get("name") or "").strip()
            if name.lower() != scheme_name.lower():
                continue
            detected = _theme_from_hex_background(scheme.get("background"))
            if detected:
                _theme_debug(f'active scheme="{scheme_name}" -> {detected}')
                return detected
        detected = _windows_terminal_builtin_scheme_theme(scheme_name)
        if detected:
            _theme_debug(f'active scheme="{scheme_name}" -> {detected}')
            return detected
        _theme_debug(f'active scheme="{scheme_name}" -> none')
    if has_windows_terminal:
        _theme_debug("windows-terminal default -> dark")
        return "dark"
    return None


def detect_terminal_theme_if_available(stream: Any | None = None) -> TerminalTheme | None:
    """Detect the terminal background without guessing when detection fails."""
    for env_name in ("ALYSIS_THEME", "OWL_THEME"):
        raw_value = env_get(env_name)
        requested = _normalize_theme(raw_value)
        _theme_debug(f"env={env_name} -> {requested or 'none'}")
        if requested:
            return requested

    colorfgbg = os.environ.get("COLORFGBG")
    detected = _theme_from_colorfgbg(colorfgbg)
    _theme_debug(f"COLORFGBG={colorfgbg or 'missing'} -> {detected or 'none'}")
    if detected:
        return detected

    if os.environ.get("NO_COLOR"):
        _theme_debug("OSC11=skipped (NO_COLOR)")
    elif not _truthy_env("ALYSIS_ENABLE_OSC11"):
        _theme_debug("OSC11=skipped (disabled)")
    else:
        detected = _theme_from_osc11(stream)
        if detected:
            return detected

    detected = _theme_from_windows_terminal_settings()
    if detected:
        return detected

    # macOS's global light/dark appearance does not describe a Terminal.app
    # window's active profile. A light desktop can host a dark terminal (and
    # vice versa), so using AppleInterfaceStyle here can select the exact
    # opposite foreground palette. Unknown is safer: callers then inherit the
    # terminal's own foreground and background colours.
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        _theme_debug("Apple_Terminal profile background unavailable -> none")
    return None


def detect_terminal_theme(stream: Any | None = None) -> TerminalTheme:
    """Detect the host terminal theme, returning neutral when it is unknown."""
    detected = detect_terminal_theme_if_available(stream)
    if detected:
        _theme_debug(f"result={detected}")
        return detected

    fallback = _fallback_theme()
    _theme_debug(f"result={fallback}")
    return fallback


def _fallback_theme() -> TerminalTheme:
    for env_name in ("ALYSIS_FALLBACK_THEME", "OWL_FALLBACK_THEME"):
        fallback = _normalize_theme(env_get(env_name))
        if fallback:
            return fallback
    return "neutral"


__all__ = [
    "detect_terminal_theme",
    "detect_terminal_theme_if_available",
    "_hex_to_8bit",
    "_load_jsonc_file",
    "_strip_json_comments",
    "_is_wsl",
    "_theme_from_colorfgbg",
    "_theme_from_hex_background",
    "_theme_from_osc11",
    "_theme_from_windows_terminal_settings",
    "_windows_terminal_builtin_scheme_theme",
    "_windows_terminal_scheme_name",
    "_windows_terminal_settings_paths",
]
