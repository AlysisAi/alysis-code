"""Consume the exact host-owned root outcome, independently of process status."""

from __future__ import annotations

import json
import shlex
from typing import Any


def outcome_emit_command(*, path: str, marker: str) -> str:
    script = (
        "import json; from pathlib import Path; "
        f"p=Path({path!r}); "
        f"print({marker!r}+json.dumps(json.loads(p.read_text())) if p.is_file() else '')"
    )
    return "python -c " + shlex.quote(script)


def outcome_metadata(result: Any, *, marker: str) -> dict[str, Any]:
    stdout = result.get("stdout", "") if isinstance(result, dict) else getattr(result, "stdout", "")
    for line in reversed(str(stdout or "").splitlines()):
        if not line.startswith(marker):
            continue
        try:
            record = json.loads(line[len(marker) :])
        except (ValueError, TypeError):
            break
        if (
            isinstance(record, dict)
            and record.get("terminal") is True
            and isinstance(record.get("session_id"), str)
            and bool(record["session_id"])
            and record.get("verified_success") is (record.get("outcome") == "verified_success")
            and record.get("outcome")
            in {
                "verified_success",
                "completed_unverified",
                "incomplete",
                "blocked",
                "deadline_exceeded",
                "provider_failure",
                "cancelled",
            }
        ):
            return {"alysis_task_outcome": record}
        break
    return {"alysis_task_outcome": None, "alysis_task_outcome_status": "unavailable"}
