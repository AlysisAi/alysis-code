#!/usr/bin/env python3
"""Notification hook that renders a macOS banner via osascript.

Wire it in .alysis/hooks.json:

    {
      "hooks": {
        "Notification": [
          {
            "hooks": [
              {
                "type": "command",
                "id": "notify.macos-banner",
                "command": "python docs/examples/hooks/notify_done_macos.py"
              }
            ]
          }
        ]
      }
    }

No-op on non-macOS systems.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> int:
    if platform.system() != "Darwin":
        return 0
    osascript = shutil.which("osascript")
    if osascript is None:
        return 0
    payload = _read_payload()
    message = str(payload.get("message") or "alysis")
    cause = str(payload.get("cause") or "notification")
    title = f"alysis • {cause}"
    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    try:
        subprocess.run(
            [osascript, "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
