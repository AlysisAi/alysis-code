#!/usr/bin/env python3
"""Windows-native lifecycle notification hook using winsound.

Built-in handler shipped with alysis-code; no path discovery needed.
Handles TurnComplete and Notification events.
TurnComplete with exit_code 0 plays MB_ICONASTERISK.
TurnComplete with any other exit_code plays MB_ICONHAND.
Notification with cause "pre_tool_use_blocked" plays MB_ICONEXCLAMATION.
Other Notification causes play MB_ICONQUESTION.
Other hook events exit silently.
Example .alysis/hooks.json:
    {"hooks": {"TurnComplete": [{"hooks": [{"type": "command",
      "command": "python -m alysis_code.builtin_hooks.notify_done_windows"}]}],
      "Notification": [{"hooks": [{"type": "command",
      "command": "python -m alysis_code.builtin_hooks.notify_done_windows"}]}]}}
The hook never writes stdout, so dispatcher JSON parsing stays clean.
On non-Windows or unavailable audio devices it exits 0 silently.
"""

from __future__ import annotations

import json
import sys

try:
    import winsound
except ImportError:
    winsound = None


def _read_payload() -> dict:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _beep(sound: int) -> None:
    if winsound is None:
        return
    try:
        winsound.MessageBeep(sound)
    except Exception:  # noqa: BLE001 - notification hooks must never break a turn.
        pass


def main() -> int:
    if winsound is None:
        return 0
    payload = _read_payload()
    event_name = payload.get("hook_event_name")
    if event_name == "TurnComplete":
        sound = winsound.MB_ICONASTERISK if payload.get("exit_code") == 0 else winsound.MB_ICONHAND
        _beep(sound)
    elif event_name == "Notification":
        if payload.get("cause") == "pre_tool_use_blocked":
            _beep(winsound.MB_ICONEXCLAMATION)
        else:
            _beep(winsound.MB_ICONQUESTION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
