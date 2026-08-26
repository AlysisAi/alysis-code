#!/usr/bin/env python3
"""PreToolUse hook that blocks git_apply_patch for destructive patches.

Wire it in .alysis/hooks.json:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "git_apply_patch",
            "hooks": [
              {
                "type": "command",
                "id": "policy.block-destructive-git",
                "command": "python docs/examples/hooks/block_destructive_git.py",
                "failurePolicy": "block"
              }
            ]
          }
        ]
      }
    }

Blocks patches that would delete more than 5 tracked files, unless
tool_input explicitly sets "allow_destructive": true.
"""

from __future__ import annotations

import json
import re
import sys

DESTRUCTIVE_FILE_LIMIT = 5
DELETED_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _count_deletions(patch_text: str) -> int:
    if not patch_text:
        return 0
    deletions = 0
    in_file_block = False
    current_is_deletion = False
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            if in_file_block and current_is_deletion:
                deletions += 1
            in_file_block = True
            current_is_deletion = False
            continue
        if line.startswith("deleted file mode"):
            current_is_deletion = True
    if in_file_block and current_is_deletion:
        deletions += 1
    return deletions


def main() -> int:
    payload = _read_payload()
    tool_input = payload.get("tool_input") or {}
    if tool_input.get("allow_destructive") is True:
        return 0
    patch_text = str(tool_input.get("patch") or tool_input.get("diff") or "")
    deletions = _count_deletions(patch_text)
    if deletions > DESTRUCTIVE_FILE_LIMIT:
        json.dump(
            {
                "hookSpecificOutput": {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Patch deletes {deletions} tracked files; limit is "
                        f"{DESTRUCTIVE_FILE_LIMIT}. Set allow_destructive=true "
                        f"to override."
                    ),
                }
            },
            sys.stdout,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
