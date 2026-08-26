#!/usr/bin/env python3
"""PreToolUse hook that runs ruff format on fs_write content.

Wire it in .alysis/hooks.json:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "fs_write",
            "hooks": [
              {
                "type": "command",
                "id": "format.ruff-on-write",
                "command": "python docs/examples/hooks/format_on_write.py"
              }
            ]
          }
        ]
      }
    }

It only formats .py files. For other extensions it passes the input
through unchanged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def main() -> int:
    payload = _read_payload()
    tool_input = payload.get("tool_input") or {}
    path = str(tool_input.get("path") or "")
    content = tool_input.get("content")
    if not isinstance(content, str) or not path.endswith(".py"):
        return 0
    ruff = shutil.which("ruff")
    if ruff is None:
        return 0
    try:
        proc = subprocess.run(
            [ruff, "format", "-"],
            input=content,
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return 0
    if proc.returncode != 0:
        return 0
    formatted = proc.stdout
    if formatted == content:
        return 0
    new_input = dict(tool_input)
    new_input["content"] = formatted
    json.dump(
        {
            "modifiedInput": new_input,
            "systemMessage": f"format_on_write: ruff-formatted {path}",
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
