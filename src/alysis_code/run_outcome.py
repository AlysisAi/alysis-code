from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

SUCCESS_EXIT_CODE = 0
AGENT_FAILURE_EXIT_CODE = 1
# EX_TEMPFAIL on Unix. Keep the numeric value explicit so it is stable on Windows too.
INFRASTRUCTURE_FAILURE_EXIT_CODE = 75


class RunOutcome(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    INFRA_FAIL = "infra_fail"


def run_outcome_for_exit_code(exit_code: int | None) -> RunOutcome:
    if exit_code == SUCCESS_EXIT_CODE:
        return RunOutcome.SUCCESS
    if exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE:
        return RunOutcome.INFRA_FAIL
    return RunOutcome.FAIL


def extract_process_exit_code(error: Any) -> int | None:
    """Recover a child-process exit code from common runner exceptions."""

    for attr_name in ("exit_code", "returncode", "return_code", "code"):
        value = _coerce_exit_code(getattr(error, attr_name, None))
        if value is not None:
            return value

    message = str(error or "")
    for pattern in (
        r"\bexit(?:\s+(?:code|status))?\s*[=:]?\s*(-?\d+)\b",
        r"\bexited\s+with\s+(?:code|status)\s+(-?\d+)\b",
        r"\breturn(?:code| code)\s*[=:]?\s*(-?\d+)\b",
    ):
        match = re.search(pattern, message, re.IGNORECASE)
        if match is not None:
            return _coerce_exit_code(match.group(1))
    return None


def run_outcome_metadata(exit_code: int | None) -> dict[str, int | str | None]:
    return {
        "alysis_exit_code": exit_code,
        "alysis_outcome": run_outcome_for_exit_code(exit_code).value,
    }


def _coerce_exit_code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
