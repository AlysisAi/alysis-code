from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..forge import RunPaths
from ..model_registry import ModelRegistry
from .planner_context import (
    AssetReadinessReport,
    AssetReferenceReport,
    PlannerAssetsBundle,
    PlannerAssetsContext,
    build_planner_assets_bundle,
)
from .surface import AssetSurface

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriorAssetUsageSummary:
    task_id: str
    attempts_seen: int
    primary_count: int
    may_need_count: int
    pinned_count: int
    reads_per_asset: dict[str, int]
    loads_per_asset: dict[str, int]
    inline_injections_per_asset: dict[str, int]
    text_block: str


@dataclass(frozen=True)
class ReplannerAssetsBundle:
    context: PlannerAssetsContext
    inline_image_paths: list[str]
    readiness_report: AssetReadinessReport
    reference_report: AssetReferenceReport
    prior_usage_summary: PriorAssetUsageSummary | None


def build_replanner_assets_bundle(
    *,
    cfg: AppConfig,
    surface: AssetSurface,
    plan: dict[str, Any] | None,
    failed_task_id: str | None,
    run_paths: RunPaths,
    role_model: str,
    model_registry: ModelRegistry,
) -> ReplannerAssetsBundle:
    planner_bundle = build_planner_assets_bundle(
        cfg=cfg,
        surface=surface,
        plan=plan,
        role_model=role_model,
        model_registry=model_registry,
    )
    prior_usage_summary = (
        summarize_prior_asset_usage(run_paths=run_paths, task_id=failed_task_id)
        if failed_task_id
        else None
    )
    return ReplannerAssetsBundle(
        context=planner_bundle.context,
        inline_image_paths=planner_bundle.inline_image_paths,
        readiness_report=planner_bundle.readiness_report,
        reference_report=planner_bundle.reference_report,
        prior_usage_summary=prior_usage_summary,
    )


def replanner_bundle_as_planner_bundle(bundle: ReplannerAssetsBundle) -> PlannerAssetsBundle:
    return PlannerAssetsBundle(
        context=bundle.context,
        inline_image_paths=bundle.inline_image_paths,
        readiness_report=bundle.readiness_report,
        reference_report=bundle.reference_report,
    )


def summarize_prior_asset_usage(
    *,
    run_paths: RunPaths,
    task_id: str | None,
) -> PriorAssetUsageSummary | None:
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        return None
    path = run_paths.execution_asset_usage_dir / f"{_safe_task(clean_task_id)}.jsonl"
    if not path.exists():
        return None

    reads: Counter[str] = Counter()
    loads: Counter[str] = Counter()
    inline: Counter[str] = Counter()
    primary_ids: set[str] = set()
    attempts_seen = 0
    primary_count = 0
    may_need_count = 0
    pinned_count = 0
    events_seen = 0

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        LOGGER.warning(
            "replanner_asset_usage failed to read task_id=%s path=%s: %s",
            clean_task_id,
            path,
            exc,
        )
        return None

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "replanner_asset_usage skipped malformed line task_id=%s line=%s: %s",
                clean_task_id,
                line_number,
                exc,
            )
            continue
        if not isinstance(payload, dict):
            continue
        event = str(payload.get("event") or "").strip()
        asset_id = str(payload.get("asset_id") or "").strip()
        events_seen += 1
        if event == "summary":
            attempts_seen += 1
            primary_count = max(primary_count, _safe_int(payload.get("primary_count")))
            may_need_count = max(may_need_count, _safe_int(payload.get("may_need_count")))
            pinned_count = max(pinned_count, _safe_int(payload.get("pinned_count")))
        elif event == "allocation_decision" and asset_id:
            primary_ids.add(asset_id)
        elif event == "asset_read" and asset_id:
            reads[asset_id] += 1
        elif event == "asset_load" and asset_id:
            loads[asset_id] += 1
        elif event == "inline_injection" and asset_id:
            inline[asset_id] += 1

    if events_seen == 0:
        return None
    if attempts_seen == 0:
        attempts_seen = 1
    primary_count = max(primary_count, len(primary_ids))
    text_block = _render_prior_usage_block(
        task_id=clean_task_id,
        attempts_seen=attempts_seen,
        primary_count=primary_count,
        may_need_count=may_need_count,
        pinned_count=pinned_count,
        primary_ids=sorted(primary_ids),
        reads=reads,
        loads=loads,
        inline=inline,
    )
    return PriorAssetUsageSummary(
        task_id=clean_task_id,
        attempts_seen=attempts_seen,
        primary_count=primary_count,
        may_need_count=may_need_count,
        pinned_count=pinned_count,
        reads_per_asset=dict(sorted(reads.items())),
        loads_per_asset=dict(sorted(loads.items())),
        inline_injections_per_asset=dict(sorted(inline.items())),
        text_block=text_block,
    )


def _render_prior_usage_block(
    *,
    task_id: str,
    attempts_seen: int,
    primary_count: int,
    may_need_count: int,
    pinned_count: int,
    primary_ids: list[str],
    reads: Counter[str],
    loads: Counter[str],
    inline: Counter[str],
) -> str:
    distinct_reads = len(reads)
    primary_label = f"{primary_count}"
    if primary_ids:
        primary_label += f" ({', '.join(primary_ids[:12])})"
    lines = [
        "## Prior Attempt Asset Interaction",
        "",
        f"Task `{task_id}` had {attempts_seen} prior attempt(s). During those attempts:",
        "",
        f"- Primary assets bound: {primary_label}",
        f"- May-need assets bound: {may_need_count}",
        f"- Pinned assets present: {pinned_count}",
        f"- Reads issued: {sum(reads.values())} across {distinct_reads} distinct asset(s)",
        f"- Loads issued: {sum(loads.values())}",
        f"- Image inline injections: {sum(inline.values())}",
    ]
    if reads:
        lines.extend(["", "Reads per asset:"])
        for asset_id, count in sorted(reads.items()):
            lines.append(f"  - {asset_id}: {count}")
    if loads:
        lines.extend(["", "Loads per asset:"])
        for asset_id, count in sorted(loads.items()):
            lines.append(f"  - {asset_id}: {count}")
    if inline:
        lines.extend(["", "Inline injections per asset:"])
        for asset_id, count in sorted(inline.items()):
            lines.append(f"  - {asset_id}: {count}")
    lines.extend(
        [
            "",
            "If the prior attempt's asset binding contributed to the failure, revise "
            "asset_briefing.primary or asset_briefing.may_need to address the gap.",
        ]
    )
    return "\n".join(lines).rstrip()


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_task(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_id)
    return safe or "task"
