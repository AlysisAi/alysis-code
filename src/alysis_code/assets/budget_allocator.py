from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ..atomic_io import atomic_write_json
from ..config import (
    AppConfig,
    resolve_llm_enable_thinking,
    resolve_model_access_api_key,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
    resolve_role_temperature,
)
from ..forge import RunPaths, now_iso
from ..llm.factory import make_llm_client
from ..llm.types import LLMError, LLMResponse
from ..model_registry import ModelRegistry
from ..model_router import resolve_model_for_role
from ..token_budget import estimate_tokens
from .models import AssetError
from .worker_mirror import MirroredAssetEntry, TaskAssetMirror

LOGGER = logging.getLogger(__name__)

InclusionMode = Literal["full_inline", "focused_extract", "reference_only"]
_ALLOCATOR_TOOL_NAME = "record_task_asset_allocation"
_ALLOCATOR_TOOL_CHOICE = {
    "type": "function",
    "function": {"name": _ALLOCATOR_TOOL_NAME},
}
_ALLOCATOR_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _ALLOCATOR_TOOL_NAME,
        "description": "Persist per-task asset inclusion decisions.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decisions"],
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["asset_id", "mode", "focus", "reason"],
                        "properties": {
                            "asset_id": {"type": "string"},
                            "mode": {
                                "type": "string",
                                "enum": ["full_inline", "focused_extract", "reference_only"],
                            },
                            "focus": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                            },
                            "reason": {"type": "string"},
                        },
                    },
                }
            },
        },
    },
}


@dataclass(frozen=True)
class AssetInclusionDecision:
    asset_id: str
    mode: InclusionMode
    focus: str | None
    reason: str


@dataclass(frozen=True)
class TaskAssetAllocation:
    task_id: str
    decisions: list[AssetInclusionDecision]
    elapsed_ms: int
    model: str | None
    tokens_used: dict[str, int]
    fallback_used: bool
    fallback_reason: str | None


def allocate_task_assets(
    *,
    task: dict[str, Any],
    plan: dict[str, Any],
    mirror: TaskAssetMirror,
    cfg: AppConfig,
    model_registry: ModelRegistry,
    instruction_token_budget: int,
    api_key: str | None = None,
) -> TaskAssetAllocation:
    _ = plan
    started = time.monotonic()
    task_id = str(task.get("id") or "").strip()
    primary = list(mirror.primary)
    if not primary:
        return TaskAssetAllocation(
            task_id=task_id,
            decisions=[],
            elapsed_ms=0,
            model=None,
            tokens_used={},
            fallback_used=False,
            fallback_reason=None,
        )
    role = str(cfg.assets.worker.allocator_role or "comprehension").strip().lower()
    model = resolve_model_for_role(cfg=cfg, role=role, plan=plan)
    try:
        response = _call_allocator(
            task=task,
            primary=primary,
            cfg=cfg,
            role=role,
            model=model,
            api_key=api_key,
            instruction_token_budget=instruction_token_budget,
            retry_note=None,
        )
        decisions = _parse_decisions(
            response, primary_asset_ids=[entry.asset_id for entry in primary]
        )
    except Exception as first_error:  # noqa: BLE001
        try:
            response = _call_allocator(
                task=task,
                primary=primary,
                cfg=cfg,
                role=role,
                model=model,
                api_key=api_key,
                instruction_token_budget=instruction_token_budget,
                retry_note=str(first_error),
            )
            decisions = _parse_decisions(
                response,
                primary_asset_ids=[entry.asset_id for entry in primary],
            )
        except Exception as second_error:  # noqa: BLE001
            allocation = _fallback_allocation(
                task_id=task_id,
                primary=primary,
                instruction_token_budget=instruction_token_budget,
                reason=str(second_error) or str(first_error),
                started=started,
            )
            _log_allocation(allocation)
            return allocation
    allocation = TaskAssetAllocation(
        task_id=task_id,
        decisions=decisions,
        elapsed_ms=_elapsed_ms(started),
        model=model,
        tokens_used=_usage_payload(response),
        fallback_used=False,
        fallback_reason=None,
    )
    _log_allocation(allocation)
    return allocation


def write_task_asset_allocation(
    *,
    run_paths: RunPaths,
    allocation: TaskAssetAllocation,
    started_at: str | None = None,
) -> None:
    path = run_paths.execution_asset_briefings_dir / f"{_safe_task(allocation.task_id)}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {"schema_version": 1, "task_id": allocation.task_id, "attempts": []}
    if not isinstance(payload, dict):
        payload = {"schema_version": 1, "task_id": allocation.task_id, "attempts": []}
    attempts = payload.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        payload["attempts"] = attempts
    attempts.append(
        {
            "started_at": started_at or now_iso(),
            "model": allocation.model,
            "tokens_used": dict(allocation.tokens_used),
            "elapsed_ms": allocation.elapsed_ms,
            "fallback_used": allocation.fallback_used,
            "fallback_reason": allocation.fallback_reason,
            "decisions": [asdict(decision) for decision in allocation.decisions],
        }
    )
    payload["schema_version"] = 1
    payload["task_id"] = allocation.task_id
    atomic_write_json(path, payload)


def _call_allocator(
    *,
    task: dict[str, Any],
    primary: list[MirroredAssetEntry],
    cfg: AppConfig,
    role: str,
    model: str,
    api_key: str | None,
    instruction_token_budget: int,
    retry_note: str | None,
) -> LLMResponse:
    client = make_llm_client(
        cfg=cfg,
        api_key=resolve_model_access_api_key(cfg, override=api_key),
        model=model,
        timeout_s=float(cfg.assets.worker.allocator_timeout_seconds),
        temperature=resolve_role_temperature(cfg, role=role),
        prompt_cache_key=resolve_prompt_cache_key(cfg),
        prompt_cache_retention=resolve_prompt_cache_retention(cfg),
        enable_thinking=resolve_llm_enable_thinking(cfg),
    )
    prompt = _allocation_prompt(
        task=task,
        primary=primary,
        instruction_token_budget=instruction_token_budget,
        retry_note=retry_note,
    )
    try:
        return client.chat(
            messages=[
                {"role": "system", "content": "Return the allocation tool call only."},
                {"role": "user", "content": prompt},
            ],
            temperature=resolve_role_temperature(cfg, role=role),
            tools=[_ALLOCATOR_TOOL],
            tool_choice=_ALLOCATOR_TOOL_CHOICE,
        )
    except LLMError as exc:
        if not _structured_tool_call_unsupported(exc):
            raise
        LOGGER.warning(
            "Structured tool-call asset allocation was rejected by the provider; retrying with JSON mode."
        )
        return client.chat(
            messages=[
                {"role": "system", "content": "Return a JSON object with a decisions array only."},
                {"role": "user", "content": prompt},
            ],
            temperature=resolve_role_temperature(cfg, role=role),
            response_format={"type": "json_object"},
        )


def _allocation_prompt(
    *,
    task: dict[str, Any],
    primary: list[MirroredAssetEntry],
    instruction_token_budget: int,
    retry_note: str | None,
) -> str:
    lines = [
        "Decide how each primary asset should be included in the worker task prompt.",
        "Modes:",
        "- full_inline: include full extracted text or image comprehension data.",
        "- focused_extract: include an LLM-extracted subset focused on the task.",
        "- reference_only: include only summary and let the worker call asset_read/asset_load.",
        f"Instruction token budget: {max(0, int(instruction_token_budget))}",
        "",
        "Task:",
        f"- id: {str(task.get('id') or '').strip()}",
        f"- title: {str(task.get('title') or '').strip()}",
        f"- description: {str(task.get('description') or '').strip()}",
        "",
        "Primary assets:",
    ]
    for entry in primary:
        summary = (
            entry.comprehension.data.semantic_summary
            if entry.comprehension is not None
            else "(no comprehension)"
        )
        lines.extend(
            [
                f"- asset_id: {entry.asset_id}",
                f"  kind: {entry.kind}",
                f"  status: {entry.status}",
                f"  size_bytes: {entry.size_bytes}",
                f"  rationale: {entry.rationale or ''}",
                f"  expected_use: {entry.expected_use or ''}",
                f"  summary: {summary}",
            ]
        )
    if retry_note:
        lines.extend(
            [
                "",
                "Previous response was invalid. Return exactly one decision per primary asset id.",
                f"Validation error: {retry_note}",
            ]
        )
    return "\n".join(lines)


def _parse_decisions(
    response: LLMResponse,
    *,
    primary_asset_ids: list[str],
) -> list[AssetInclusionDecision]:
    payload: dict[str, Any] | None = None
    for tool_call in response.tool_calls or []:
        if tool_call.name == _ALLOCATOR_TOOL_NAME:
            payload = tool_call.arguments
            break
    if payload is None:
        try:
            payload = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise AssetError("Allocator did not return a JSON allocation.") from exc
    raw_decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw_decisions, list):
        raise AssetError("Allocator decisions must be an array.")
    decisions: list[AssetInclusionDecision] = []
    for index, raw in enumerate(raw_decisions):
        if not isinstance(raw, dict):
            raise AssetError(f"Allocator decision {index} must be an object.")
        asset_id = str(raw.get("asset_id") or "").strip()
        mode = str(raw.get("mode") or "").strip()
        if mode not in {"full_inline", "focused_extract", "reference_only"}:
            raise AssetError(f"Allocator returned invalid mode for {asset_id}.")
        focus = raw.get("focus")
        focus_text = str(focus).strip() if focus is not None else None
        if mode == "focused_extract" and not focus_text:
            raise AssetError(f"Allocator focused_extract for {asset_id} requires focus.")
        if mode != "focused_extract":
            focus_text = None
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise AssetError(f"Allocator decision for {asset_id} requires reason.")
        decisions.append(
            AssetInclusionDecision(
                asset_id=asset_id,
                mode=mode,  # type: ignore[arg-type]
                focus=focus_text,
                reason=reason,
            )
        )
    expected = set(primary_asset_ids)
    actual = {decision.asset_id for decision in decisions}
    if actual != expected or len(decisions) != len(expected):
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise AssetError(
            "Allocator decisions must cover exactly the primary assets. "
            f"missing={missing}; extra={extra}"
        )
    return decisions


def _fallback_allocation(
    *,
    task_id: str,
    primary: list[MirroredAssetEntry],
    instruction_token_budget: int,
    reason: str,
    started: float,
) -> TaskAssetAllocation:
    threshold = max(0, int(instruction_token_budget * 0.5))
    decisions_by_id: dict[str, AssetInclusionDecision] = {}
    used_tokens = 0
    for entry in sorted(primary, key=lambda item: (max(0, item.size_bytes), item.asset_id)):
        content_tokens = estimate_tokens(_inline_candidate_text(entry))
        if entry.status == "mirrored" and used_tokens + content_tokens <= threshold:
            used_tokens += content_tokens
            mode: InclusionMode = "full_inline"
            decision_reason = "Fallback selected small assets for inline context first."
        else:
            mode = "reference_only"
            decision_reason = "Fallback kept this asset available by reference."
        decisions_by_id[entry.asset_id] = AssetInclusionDecision(
            asset_id=entry.asset_id,
            mode=mode,
            focus=None,
            reason=decision_reason,
        )
    allocation = TaskAssetAllocation(
        task_id=task_id,
        decisions=[decisions_by_id[entry.asset_id] for entry in primary],
        elapsed_ms=_elapsed_ms(started),
        model=None,
        tokens_used={},
        fallback_used=True,
        fallback_reason=reason[:500],
    )
    LOGGER.info("asset_allocator_fallback task_id=%s reason=%s", task_id, "allocator_failed")
    return allocation


def _inline_candidate_text(entry: MirroredAssetEntry) -> str:
    if entry.extracted_text_workspace_path is not None:
        try:
            return entry.extracted_text_workspace_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return ""
    if entry.comprehension is not None:
        return entry.comprehension.data.semantic_summary
    return ""


def _usage_payload(response: LLMResponse) -> dict[str, int]:
    usage = response.usage
    if usage is None:
        return {}
    payload: dict[str, int] = {}
    if usage.prompt_tokens is not None:
        payload["input"] = int(usage.prompt_tokens)
    if usage.completion_tokens is not None:
        payload["output"] = int(usage.completion_tokens)
    return payload


def _log_allocation(allocation: TaskAssetAllocation) -> None:
    counts = {"full_inline": 0, "focused_extract": 0, "reference_only": 0}
    for decision in allocation.decisions:
        counts[decision.mode] += 1
    LOGGER.info(
        "asset_allocator task_id=%s model=%s elapsed_ms=%s primary=%s full_inline=%s "
        "focused=%s reference=%s fallback=%s",
        allocation.task_id,
        allocation.model,
        allocation.elapsed_ms,
        len(allocation.decisions),
        counts["full_inline"],
        counts["focused_extract"],
        counts["reference_only"],
        str(allocation.fallback_used).lower(),
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _safe_task(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_id)
    return safe or "task"


def _structured_tool_call_unsupported(error: LLMError) -> bool:
    text = str(error).casefold()
    if "tool" not in text and "function" not in text:
        return False
    return any(token in text for token in ("unsupported", "unknown", "invalid", "not allowed"))
