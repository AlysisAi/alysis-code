#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    payload = json.load(sys.stdin)
    reason = str(payload.get("reason") or "completed")
    session_id = str(payload.get("session_id") or "unknown")
    title = "Alysis Code"
    body = f"Turn finished ({reason}) for {session_id}"
    subprocess.run(
        ["notify-send", title, body],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
