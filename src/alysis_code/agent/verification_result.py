"""Concise verification messages; execution and durable evidence retain full results."""

from __future__ import annotations

from typing import Any


def verification_result_for_model(result: dict[str, Any]) -> dict[str, Any]:
    """Omit parser internals and exact duplicates, without summarizing check output.

    This is a presentation boundary after the runtime has consumed the complete
    result. Unknown fields, verdicts, failures, provenance, scope, mutation data
    and artifact handles are preserved. The full result is retained in the
    tool-result event; only its model-facing content uses this representation.
    """
    commands = result.get("command_results")
    if not isinstance(commands, list) or not all(isinstance(row, dict) for row in commands):
        return result

    projected = dict(result)
    projected_commands = []
    for row in commands:
        compact = dict(row)
        compact.pop("host_test_report", None)
        if "command" in row and row.get("effective_command") == row["command"]:
            compact.pop("effective_command", None)
        projected_commands.append(compact)
    projected["command_results"] = projected_commands
    if commands and result.get("commands") == [row.get("command") for row in commands]:
        projected.pop("commands", None)

    specs = result.get("verification_command_specs")
    if isinstance(specs, list) and all(isinstance(spec, dict) for spec in specs):
        projected_specs = []
        for spec in specs:
            compact = dict(spec)
            # The command, provenance, requirement, working directory and
            # validation remain visible. IDs and parser representations are
            # consumed by the host and need not recur in conversation history.
            for key in ("command_id", "execution_mode", "timeout_policy", "argv"):
                compact.pop(key, None)
            if "original_text" in spec and spec.get("display_text") == spec["original_text"]:
                compact.pop("display_text", None)
            projected_specs.append(compact)
        projected["verification_command_specs"] = projected_specs

    evidence = result.get("verification_evidence_records")
    if isinstance(evidence, list) and all(isinstance(row, dict) for row in evidence):
        projected_evidence = []
        for row in evidence:
            compact = dict(row)
            matched = row.get("matched_command")
            if matched is not None and row.get("covered_verification_commands") == [matched]:
                compact.pop("matched_command", None)
            projected_evidence.append(compact)
        projected["verification_evidence_records"] = projected_evidence

    return projected if projected != result else result
