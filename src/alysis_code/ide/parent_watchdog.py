"""Exit when the IDE extension host that spawned this bridge is gone.

The bridge normally ends when its stdin reaches EOF, which a dead parent produces.
This is the backstop for the cases where that path stalls: a job thread ignoring
cancellation past the shutdown timeout, or a pipe handle kept open by an
unrelated process. ``ALYSIS_PARENT_PID`` is set only by the IDE extension, so the
watchdog is inert for every other way of running the CLI.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Mapping

PARENT_PID_ENV = "ALYSIS_PARENT_PID"
DEFAULT_POLL_SECONDS = 1.0
_ORPHAN_EXIT_CODE = 1


def parent_pid_from_env(environ: Mapping[str, str] | None = None) -> int | None:
    env = os.environ if environ is None else environ
    raw = (env.get(PARENT_PID_ENV) or "").strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    if pid <= 0 or pid == os.getpid():
        return None
    return pid


def process_is_alive(pid: int) -> bool:
    """True unless the process is known to be gone. Unsure means alive."""
    if sys.platform == "win32":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_is_alive(pid: int) -> bool:  # pragma: no cover - exercised on Windows
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # No such process is the only failure that proves death; access denied
        # means it exists under another account, so keep the bridge alive.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def start_parent_watchdog(
    *,
    grace_seconds: float,
    environ: Mapping[str, str] | None = None,
    is_alive: Callable[[int], bool] = process_is_alive,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    on_parent_exit: Callable[[int], None] | None = None,
    exit_process: Callable[[int], None] = os._exit,
) -> threading.Thread | None:
    """Start a daemon thread that force-exits once the parent is gone.

    Sequence on parent death: ``on_parent_exit`` (the bridge's graceful ``close``),
    then ``grace_seconds`` for the normal EOF path to finish, then ``exit_process``.
    A normal exit inside the grace window simply ends the daemon thread with it.
    Returns ``None`` when ``ALYSIS_PARENT_PID`` is unset.
    """
    pid = parent_pid_from_env(environ)
    if pid is None:
        return None

    def watch() -> None:
        while True:
            try:
                alive = is_alive(pid)
            except Exception:
                # A detection bug must never take down a healthy bridge.
                return
            if not alive:
                break
            time.sleep(poll_seconds)
        if on_parent_exit is not None:
            try:
                on_parent_exit(pid)
            except Exception:
                pass
        time.sleep(max(0.0, grace_seconds))
        exit_process(_ORPHAN_EXIT_CODE)

    thread = threading.Thread(target=watch, name="alysis-parent-watchdog", daemon=True)
    thread.start()
    return thread
