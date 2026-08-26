from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .session_store import read_session_events
from .tools.registry import builtin_tool_names_with_category
from .web_research import build_web_research_metrics_from_events

_READ_TOOLS = frozenset(builtin_tool_names_with_category("read"))
_WRITE_TOOLS = frozenset(builtin_tool_names_with_category("write"))

_TEST_CMD_HINTS = [
    "pytest",
    "unittest",
    "nose",
    "go test",
    "cargo test",
    "npm test",
    "pnpm test",
    "yarn test",
    "vitest",
    "jest",
    "phpunit",
    "mvn test",
    "gradle test",
    "ctest",
]


def _extract_shell_command(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    value = arguments.get("cmd") or arguments.get("command")
    if isinstance(value, str):
        return value.strip()
    return ""


def _is_test_command(cmd: str) -> bool:
    lowered = cmd.lower()
    return any(hint in lowered for hint in _TEST_CMD_HINTS)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _custom_tool_capabilities(payload: dict[str, Any]) -> dict[str, Any] | None:
    custom_tool = payload.get("custom_tool")
    if not isinstance(custom_tool, dict):
        return None
    capabilities = custom_tool.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    return capabilities


def _custom_tool_categories_and_risk(payload: dict[str, Any]) -> tuple[set[str], str | None]:
    capabilities = _custom_tool_capabilities(payload)
    if capabilities is None:
        return set(), None
    categories: set[str] = {"custom_tool"}
    read_only = bool(capabilities.get("read_only"))
    destructive = bool(capabilities.get("destructive"))
    network_access = str(capabilities.get("network_access") or "unspecified").strip().lower()
    filesystem_read_scope = (
        str(capabilities.get("filesystem_read_scope") or "unspecified").strip().lower()
    )
    filesystem_write_scope = (
        str(capabilities.get("filesystem_write_scope") or "unspecified").strip().lower()
    )

    if read_only:
        categories.add("read")
    if filesystem_read_scope not in {"none", "unspecified"}:
        categories.update({"read", "filesystem"})
    if filesystem_write_scope not in {"none", "unspecified"}:
        categories.update({"write", "filesystem"})
    if network_access not in {"none", "unspecified"}:
        categories.add("network")
    if destructive:
        categories.update({"write", "destructive"})

    if destructive:
        risk = "destructive"
    elif filesystem_write_scope not in {"none", "unspecified"}:
        risk = "write"
    elif network_access not in {"none", "unspecified"}:
        risk = "network"
    elif read_only:
        risk = "read_only"
    else:
        risk = "unspecified"
    return categories, risk


def score_session_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    event_list = list(events)
    tool_counts: Counter[str] = Counter()
    tool_category_counts: Counter[str] = Counter()
    tool_risk_counts: Counter[str] = Counter()
    custom_tool_counts: Counter[str] = Counter()
    custom_tool_risk_counts: Counter[str] = Counter()
    repeated_errors: Counter[str] = Counter()
    tool_sequence: list[tuple[int | None, str, frozenset[str]]] = []
    test_shell_commands: list[str] = []

    first_write_step: int | None = None
    read_before_first_write: bool | None = None

    metrics: dict[str, Any] = {
        "session_id": "",
        "tool_calls": 0,
        "tool_errors": 0,
        "write_calls": 0,
        "blocked_write_errors": 0,
        "approval_eof_errors": 0,
        "shell_runs": 0,
        "verify_runs": 0,
        "test_shell_runs": 0,
        "custom_tool_calls": 0,
        "llm_usage_events": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "known_cost_calls": 0,
        "unknown_cost_calls": 0,
        "tool_counts": {},
        "tool_category_counts": {},
        "tool_risk_counts": {},
        "custom_tool_counts": {},
        "custom_tool_risk_counts": {},
        "test_shell_commands": [],
        "first_write_step": None,
        "read_before_first_write": None,
        "system_prompt_sha256": "",
        "has_system_prompt_sha256": False,
        "verification_selection_source": "",
        "verification_selection_reason": "",
        "verification_contract_type": "",
        "verification_authoritative": False,
        "authoritative_verification_failures": 0,
        "non_authoritative_verification_failures": 0,
        "last_verification_failure_kind": "",
        "web_search_calls": 0,
        "web_fetch_calls": 0,
        "unique_web_queries": 0,
        "unique_web_fetch_urls": 0,
        "duplicate_web_queries": 0,
        "duplicate_web_fetches": 0,
        "total_web_sources_returned": 0,
        "total_web_sources_fetched": 0,
        "repeated_tool_errors": [],
    }

    for event in event_list:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

        if event_type == "session_start":
            metrics["session_id"] = str(event.get("session_id") or metrics["session_id"] or "")
            prompt_sha = str(payload.get("system_prompt_sha256") or "").strip()
            if prompt_sha:
                metrics["system_prompt_sha256"] = prompt_sha
                metrics["has_system_prompt_sha256"] = True
            for key in (
                "verification_selection_source",
                "verification_selection_reason",
                "verification_contract_type",
            ):
                value = str(payload.get(key) or "").strip()
                if value:
                    metrics[key] = value
            metrics["verification_authoritative"] = bool(
                payload.get("verification_authoritative", metrics["verification_authoritative"])
            )
            continue

        if event_type == "verification_contract_updated":
            for key in (
                "verification_selection_source",
                "verification_selection_reason",
                "verification_contract_type",
            ):
                value = str(payload.get(key) or "").strip()
                if value:
                    metrics[key] = value
            metrics["verification_authoritative"] = bool(
                payload.get("verification_authoritative", metrics["verification_authoritative"])
            )
            continue

        if event_type == "tool_call":
            name = str(payload.get("name") or "").strip()
            step_value = payload.get("step")
            step = step_value if isinstance(step_value, int) else None
            if not name:
                continue

            metrics["tool_calls"] += 1
            tool_counts[name] += 1

            custom_categories, custom_risk = _custom_tool_categories_and_risk(payload)
            categories = set(custom_categories)
            if name in _READ_TOOLS:
                categories.add("read")
            if name in _WRITE_TOOLS:
                categories.add("write")
            for category in sorted(categories):
                tool_category_counts[category] += 1
            if custom_risk:
                tool_risk_counts[custom_risk] += 1
            elif name in _WRITE_TOOLS:
                tool_risk_counts["write"] += 1
            elif name in _READ_TOOLS:
                tool_risk_counts["read_only"] += 1
            else:
                tool_risk_counts["unspecified"] += 1

            if payload.get("tool_type") == "custom_tool" or custom_categories:
                metrics["custom_tool_calls"] += 1
                custom_tool_counts[name] += 1
                custom_tool_risk_counts[custom_risk or "unspecified"] += 1

            if name in _WRITE_TOOLS or "write" in categories:
                metrics["write_calls"] += 1
                if first_write_step is None:
                    first_write_step = step
                    if step is None:
                        read_before_first_write = False
                    else:
                        read_before_first_write = any(
                            prev_step is not None
                            and prev_step < step
                            and ("read" in prev_categories or prev_name in _READ_TOOLS)
                            for prev_step, prev_name, prev_categories in tool_sequence
                        )

            tool_sequence.append((step, name, frozenset(categories)))

            if name == "shell_run":
                metrics["shell_runs"] += 1
                cmd = _extract_shell_command(payload.get("arguments"))
                if cmd and _is_test_command(cmd):
                    metrics["test_shell_runs"] += 1
                    if cmd not in test_shell_commands:
                        test_shell_commands.append(cmd)
            if name == "verify_run":
                metrics["verify_runs"] += 1
            continue

        if event_type == "tool_result":
            name = str(payload.get("name") or "").strip()
            result = payload.get("result")
            if isinstance(result, dict) and "error" in result:
                err = str(result.get("error") or "").strip()
                metrics["tool_errors"] += 1
                repeated_errors[f"{name}: {err}"] += 1
                if "blocked write" in err.lower():
                    metrics["blocked_write_errors"] += 1
                if "eof when reading a line" in err.lower():
                    metrics["approval_eof_errors"] += 1
            continue

        if event_type == "verify_run":
            authoritative = bool(
                payload.get("verification_authoritative", metrics["verification_authoritative"])
            )
            all_passed = payload.get("all_passed")
            if all_passed is False:
                if authoritative:
                    metrics["authoritative_verification_failures"] += 1
                    metrics["last_verification_failure_kind"] = "authoritative"
                else:
                    metrics["non_authoritative_verification_failures"] += 1
                    metrics["last_verification_failure_kind"] = "non_authoritative"
            continue

        if event_type == "llm_usage":
            metrics["llm_usage_events"] += 1
            metrics["prompt_tokens"] += _as_int(payload.get("prompt_tokens"))
            metrics["completion_tokens"] += _as_int(payload.get("completion_tokens"))
            metrics["total_tokens"] += _as_int(payload.get("total_tokens"))
            # Only sum metered costs. An unmetered call (cost_usd == null) must
            # not be silently counted as $0.00, or the persisted session score
            # reports a fake definitive total; track it separately instead.
            cost_value = payload.get("cost_usd")
            if cost_value is None:
                metrics["unknown_cost_calls"] += 1
            else:
                metrics["cost_usd"] += _as_float(cost_value)
                metrics["known_cost_calls"] += 1

    metrics["tool_counts"] = dict(sorted(tool_counts.items()))
    metrics["tool_category_counts"] = dict(sorted(tool_category_counts.items()))
    metrics["tool_risk_counts"] = dict(sorted(tool_risk_counts.items()))
    metrics["custom_tool_counts"] = dict(sorted(custom_tool_counts.items()))
    metrics["custom_tool_risk_counts"] = dict(sorted(custom_tool_risk_counts.items()))
    metrics["test_shell_commands"] = test_shell_commands
    metrics["first_write_step"] = first_write_step
    metrics["read_before_first_write"] = read_before_first_write

    repeated_list: list[dict[str, Any]] = []
    for key, count in sorted(repeated_errors.items()):
        if count >= 2:
            repeated_list.append({"error": key, "count": count})
    metrics["repeated_tool_errors"] = repeated_list
    metrics.update(build_web_research_metrics_from_events(event_list))

    return metrics


def score_session_log(path: Path) -> dict[str, Any]:
    metrics = score_session_events(read_session_events(path))
    metrics["session_id"] = str(metrics.get("session_id") or path.stem)
    metrics["path"] = str(path)
    return metrics
