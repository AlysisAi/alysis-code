#!/usr/bin/env python3
"""PreToolUse hook that blocks writes containing likely secrets.

Scans fs_write, fs_patch, and fs_edit payloads before content lands.
Detects API keys, cloud access keys, GitHub/GitLab tokens, Slack tokens,
and private key block headers.
Allow intentional fixtures by adding "pragma: allowlist secret" on the
matched line or the line immediately before it.
Example .alysis/hooks.json:
    {"hooks": {"PreToolUse": [{"matcher": "fs_write|fs_patch|fs_edit",
      "hooks": [{"type": "command", "id": "security.secret-scanner",
      "command": "python docs/examples/hooks/secret_scanner.py",
      "failurePolicy": "warn"}]}]}}
Fake docs should use obvious placeholders such as sk-FAKE-EXAMPLE.
"""

from __future__ import annotations

import json
import re
import sys

PATTERNS = [
    (r"sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{32,}", "Anthropic API key"),
    (r"sk-proj-[A-Za-z0-9_-]{40,}", "OpenAI project API key"),
    (r"sk-[A-Za-z0-9]{48,}", "OpenAI API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key ID"),
    (r"AIza[0-9A-Za-z\-_]{35}", "Google API key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
    (r"gho_[A-Za-z0-9]{36}", "GitHub OAuth token"),
    (r"github_pat_[A-Za-z0-9_]{82}", "GitHub fine-grained PAT"),
    (r"glpat-[A-Za-z0-9\-_]{20}", "GitLab personal access token"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----", "Private key block"),
]
ALLOWLIST_MARKER = "pragma: allowlist secret"


def _read_payload() -> dict:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _scan_content(payload: dict) -> tuple[str, str]:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return "", ""
    if tool_name == "fs_write":
        content = tool_input.get("content")
    elif tool_name == "fs_patch":
        content = tool_input.get("patch")
    elif tool_name == "fs_edit":
        parts = [tool_input.get("old_string"), tool_input.get("new_string")]
        content = "\n".join(part for part in parts if isinstance(part, str))
    else:
        return "", ""
    return tool_name if isinstance(tool_name, str) else "", content if isinstance(
        content, str
    ) else ""


def _allowlisted(lines: list[str], index: int) -> bool:
    current = lines[index].lower()
    previous = lines[index - 1].lower() if index > 0 else ""
    return ALLOWLIST_MARKER in current or ALLOWLIST_MARKER in previous


def _first_match_label(content: str) -> str | None:
    lines = content.splitlines() or [content]
    for raw_pattern, label in PATTERNS:
        pattern = re.compile(raw_pattern)
        for index, line in enumerate(lines):
            if pattern.search(line) and not _allowlisted(lines, index):
                return label
    return None


def main() -> int:
    tool_name, content = _scan_content(_read_payload())
    if not tool_name or not content:
        return 0
    try:
        label = _first_match_label(content)
    except re.error as exc:
        sys.stderr.write(f"secret_scanner: invalid regex pattern: {exc}\n")
        return 0
    if label:
        reason = f"Detected {label} in {tool_name} content"
        print(json.dumps({"decision": "block", "reason": reason}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
