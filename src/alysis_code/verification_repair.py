"""Verification outcomes that never throw away work.

Two separate things used to collapse into the same "task failed" verdict:

* **Verification could not run.** No authoritative command exists for the task --
  ``verify_gate`` deliberately suppresses generic fallbacks and empties the command
  list for docs-only, static-web, and CI-only workspaces. The files were written and
  are on disk, but strict mode reported a plain failure, and the swarm's failure
  cleanup then deleted the worktree. Missing tooling is not a defect in the work, so
  it now produces :data:`TASK_STATUS_COMPLETED_UNVERIFIED` instead.
* **Verification ran and failed.** That *is* a real signal about the work, but a
  single shot at it is a waste: the failing command's own output is usually enough
  for the executing agent to fix the problem. :func:`run_verification_repair_loop`
  gives it a bounded number of tries before the task is failed.

This module is deliberately a stdlib-only leaf: it holds the policy (how many
attempts, when to stop, what the repair prompt says) while the caller keeps every
side effect (running the agent, committing, re-running verification). That split is
what makes the loop unit-testable without a git repository or a provider.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# Plan status for "the work landed, but nothing authoritative could check it".
# Not a failure: the task is terminal-successful for progress and dependency
# purposes, and its files/branch are kept.
TASK_STATUS_COMPLETED_UNVERIFIED = "completed_unverified"

DEFAULT_VERIFICATION_REPAIR_ATTEMPTS = 2
VERIFICATION_REPAIR_ATTEMPTS_ENV = "ALYSIS_VERIFY_REPAIR_ATTEMPTS"

# A ceiling, not a policy: a mistyped budget should not turn one task into an
# unbounded provider spend loop.
MAX_VERIFICATION_REPAIR_ATTEMPTS = 10

# Per-command excerpt budget for the repair prompt. Enough to carry a traceback and
# the assertion that produced it; small enough that three failing commands still fit
# beside the task instruction.
DEFAULT_COMMAND_OUTPUT_CHARS = 4000


class _VerifyCommandResultLike(Protocol):
    command: str
    exit_code: int
    output: str

    @property
    def ok(self) -> bool: ...


class _VerifyRunResultLike(Protocol):
    command_results: list[Any]

    @property
    def all_passed(self) -> bool: ...

    @property
    def summary(self) -> str: ...

    @property
    def failed_commands(self) -> list[str]: ...


def resolve_repair_attempt_budget(
    override: int | None = None,
    *,
    env: dict[str, str] | None = None,
) -> int:
    """Return how many repair attempts a failing verification gets.

    Precedence: explicit override (CLI flag) > environment > default. Negative and
    unparseable values fall back rather than raise -- a bad budget must not be the
    reason a task cannot run at all.
    """
    if override is not None:
        return _clamp_attempts(override)
    raw = (env if env is not None else os.environ).get(VERIFICATION_REPAIR_ATTEMPTS_ENV)
    if raw is not None and str(raw).strip():
        try:
            return _clamp_attempts(int(str(raw).strip()))
        except (TypeError, ValueError):
            return DEFAULT_VERIFICATION_REPAIR_ATTEMPTS
    return DEFAULT_VERIFICATION_REPAIR_ATTEMPTS


def _clamp_attempts(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_VERIFICATION_REPAIR_ATTEMPTS
    if parsed < 0:
        return 0
    return min(parsed, MAX_VERIFICATION_REPAIR_ATTEMPTS)


@dataclass(frozen=True)
class RepairAttemptExecution:
    """What the caller actually did for one repair attempt.

    ``verify_result`` is the re-run verification, or ``None`` when the attempt could
    not get far enough to re-verify (agent crash, commit failure).
    """

    agent_exit_code: int
    verify_result: Any | None
    committed: bool = False
    changed_files: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class VerificationRepairAttempt:
    """One recorded attempt, for the report, the event stream, and the artifact."""

    attempt: int
    agent_exit_code: int
    committed: bool
    verification_passed: bool
    verification_summary: str
    failed_commands: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "agent_exit_code": self.agent_exit_code,
            "committed": bool(self.committed),
            "verification_passed": bool(self.verification_passed),
            "verification_summary": self.verification_summary,
            "failed_commands": list(self.failed_commands),
            "changed_files": list(self.changed_files),
            "error": self.error,
        }


@dataclass(frozen=True)
class VerificationRepairOutcome:
    """Result of the whole loop.

    ``final_result`` is the verification result the task's outcome is decided on:
    the last one that actually ran, which is the initial result when no attempt
    re-verified.
    """

    final_result: Any
    attempts: tuple[VerificationRepairAttempt, ...] = ()
    skipped_reason: str | None = None

    @property
    def passed(self) -> bool:
        result = self.final_result
        return bool(getattr(result, "all_passed", False))

    @property
    def attempts_used(self) -> int:
        return len(self.attempts)

    @property
    def repaired(self) -> bool:
        """True when a repair attempt is what turned verification green."""
        return self.passed and bool(self.attempts)

    @property
    def exhausted(self) -> bool:
        """True when attempts were spent and verification still fails."""
        return bool(self.attempts) and not self.passed

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempts_used": self.attempts_used,
            "passed": self.passed,
            "repaired": self.repaired,
            "exhausted": self.exhausted,
            "skipped_reason": self.skipped_reason,
            "attempts": [item.to_payload() for item in self.attempts],
        }

    def report_lines(self) -> list[str]:
        """Human-readable lines for the task report / warning list."""
        if self.skipped_reason and not self.attempts:
            return [f"Verification repair skipped: {self.skipped_reason}"]
        lines: list[str] = []
        for item in self.attempts:
            state = "passed" if item.verification_passed else "still failing"
            line = f"Repair attempt {item.attempt}: {state} -- {item.verification_summary}"
            if item.error:
                line += f" (attempt error: {item.error})"
            lines.append(line)
        if self.skipped_reason:
            lines.append(f"Verification repair stopped: {self.skipped_reason}")
        return lines


def run_verification_repair_loop(
    *,
    initial_result: Any,
    max_attempts: int,
    attempt_repair: Callable[[int, Any], RepairAttemptExecution],
    repairable: Callable[[Any], bool] | None = None,
    on_attempt: Callable[[VerificationRepairAttempt], None] | None = None,
) -> VerificationRepairOutcome:
    """Re-run the executing agent against its own failing verification output.

    The caller's ``attempt_repair(attempt_number, failing_result)`` does the work --
    build the prompt, run the agent, commit, re-verify -- and returns what happened.
    This function owns only the decision to keep going.

    An attempt that cannot re-verify (agent crashed, nothing committed, commit
    failed) ends the loop: without a fresh verification result there is nothing new
    to feed the next attempt, so spending the remaining budget would just repeat the
    same prompt.
    """
    budget = _clamp_attempts(max_attempts)
    if getattr(initial_result, "all_passed", False):
        return VerificationRepairOutcome(final_result=initial_result)
    if budget <= 0:
        return VerificationRepairOutcome(
            final_result=initial_result,
            skipped_reason="no repair attempts are configured",
        )
    if repairable is not None and not repairable(initial_result):
        return VerificationRepairOutcome(
            final_result=initial_result,
            skipped_reason="the verification failure is not repairable by editing code",
        )

    attempts: list[VerificationRepairAttempt] = []
    current = initial_result
    skipped_reason: str | None = None

    for attempt_number in range(1, budget + 1):
        execution = attempt_repair(attempt_number, current)
        result = execution.verify_result
        passed = bool(getattr(result, "all_passed", False)) if result is not None else False
        record = VerificationRepairAttempt(
            attempt=attempt_number,
            agent_exit_code=int(execution.agent_exit_code),
            committed=bool(execution.committed),
            verification_passed=passed,
            verification_summary=(
                str(getattr(result, "summary", ""))
                if result is not None
                else "verification did not re-run"
            ),
            failed_commands=tuple(
                str(item) for item in (getattr(result, "failed_commands", ()) or ())
            ),
            changed_files=tuple(execution.changed_files),
            error=execution.error,
        )
        attempts.append(record)
        if on_attempt is not None:
            on_attempt(record)
        if result is not None:
            current = result
        if passed:
            break
        if result is None:
            skipped_reason = execution.error or "the repair attempt did not re-run verification"
            break
        if repairable is not None and not repairable(result):
            skipped_reason = "the verification failure is not repairable by editing code"
            break

    return VerificationRepairOutcome(
        final_result=current,
        attempts=tuple(attempts),
        skipped_reason=skipped_reason,
    )


def verification_failure_excerpts(
    result: Any,
    *,
    max_chars: int = DEFAULT_COMMAND_OUTPUT_CHARS,
    max_commands: int = 5,
) -> list[tuple[str, int, str]]:
    """Return ``(command, exit_code, output excerpt)`` for each failing command.

    The tail of the output is kept, not the head: a test runner puts the failure
    summary last, and the first lines are usually collection noise.
    """
    excerpts: list[tuple[str, int, str]] = []
    for item in list(getattr(result, "command_results", None) or []):
        if getattr(item, "ok", False):
            continue
        command = str(getattr(item, "command", "") or "")
        exit_code = int(getattr(item, "exit_code", 1) or 0)
        excerpts.append(
            (command, exit_code, _tail(str(getattr(item, "output", "") or ""), max_chars))
        )
        if len(excerpts) >= max_commands:
            break
    return excerpts


def _tail(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return "[... earlier output truncated ...]\n" + text[-max_chars:]


def build_repair_instruction(
    *,
    base_instruction: str,
    task_id: str,
    attempt: int,
    max_attempts: int,
    verify_summary: str,
    excerpts: Sequence[tuple[str, int, str]],
    artifact_path: str | None = None,
) -> str:
    """Append the failing verification output to the task's own instruction.

    The base instruction is repeated verbatim because a repair attempt runs in a
    fresh session: without it the agent would be asked to fix a failure for a task
    it has no context on.
    """
    lines: list[str] = [
        "# Verification Repair Attempt",
        "",
        f"Task: {task_id}",
        f"Repair attempt {attempt} of {max_attempts}.",
        "",
        "Your previous changes for this task are already committed, but the "
        "authoritative verification commands below FAILED against them.",
        "",
        f"Verification summary: {verify_summary}",
        "",
        "## Failing verification output",
        "",
    ]
    if not excerpts:
        lines.append("(no per-command output was captured)")
        lines.append("")
    for command, exit_code, output in excerpts:
        lines.append(f"### `{command}` (exit code {exit_code})")
        lines.append("")
        lines.append("```")
        lines.append(output.rstrip("\n") if output.strip() else "(no output captured)")
        lines.append("```")
        lines.append("")
    if artifact_path:
        lines.append(f"Full verification log: {artifact_path}")
        lines.append("")
    lines.extend(
        [
            "## What to do now",
            "",
            "1. Read the failing output above and find the actual cause.",
            "2. Fix it with the smallest correct change, inside this task's write scope.",
            "3. Do not weaken, skip, or delete the failing checks to make them pass.",
            "4. Do not re-do work that already succeeded; only repair what the output shows.",
            "",
            "Verification will be re-run automatically after this attempt.",
            "",
            "---",
            "",
            "# Original Task",
            "",
        ]
    )
    return "\n".join(lines) + base_instruction


__all__ = [
    "DEFAULT_COMMAND_OUTPUT_CHARS",
    "DEFAULT_VERIFICATION_REPAIR_ATTEMPTS",
    "MAX_VERIFICATION_REPAIR_ATTEMPTS",
    "TASK_STATUS_COMPLETED_UNVERIFIED",
    "VERIFICATION_REPAIR_ATTEMPTS_ENV",
    "RepairAttemptExecution",
    "VerificationRepairAttempt",
    "VerificationRepairOutcome",
    "build_repair_instruction",
    "resolve_repair_attempt_budget",
    "run_verification_repair_loop",
    "verification_failure_excerpts",
]
