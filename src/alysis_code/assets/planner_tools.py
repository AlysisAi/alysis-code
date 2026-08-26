from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import AppConfig
from ..forge import RunPaths
from ..llm.factory import make_llm_client
from ..model_registry import ModelRegistry
from .asset_read_core import perform_asset_read
from .models import AssetError, ComprehensionRecord
from .surface import AssetSurface

LOGGER = logging.getLogger(__name__)

ASSET_READ_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "asset_read",
        "description": (
            "Read the extracted text of an asset, optionally focused on a topic the planner cares about."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id"],
            "properties": {
                "asset_id": {"type": "string"},
                "focus": {
                    "type": "string",
                    "description": (
                        "Optional topic to focus the extraction on. When provided, runs a small LLM "
                        "extraction pass to return only the relevant content."
                    ),
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Cap on returned text. Default 8000.",
                    "minimum": 100,
                    "maximum": 32000,
                },
            },
        },
    },
}

ASSET_INSPECT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "asset_inspect",
        "description": "Request a re-comprehension of an asset using a specific angle or framing.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id"],
            "properties": {
                "asset_id": {"type": "string"},
                "angle": {
                    "type": "string",
                    "description": (
                        "Optional framing for the re-comprehension, for example security perspective "
                        "or data-flow focus."
                    ),
                },
            },
        },
    },
}

PLANNER_ASSET_TOOLS: list[dict[str, Any]] = [ASSET_READ_TOOL, ASSET_INSPECT_TOOL]
_INSPECT_CAP_PER_ASSET = 3


class PlannerAssetToolRunner:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        run_paths: RunPaths,
        surface: AssetSurface,
        model_registry: ModelRegistry,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.run_paths = run_paths
        self.surface = surface
        self.model_registry = model_registry
        self.api_key = api_key
        self.transport = transport
        self._inspect_counts: dict[str, int] = {}

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "asset_read":
            return self.asset_read(
                asset_id=str(arguments.get("asset_id") or ""),
                focus=_optional_string(arguments.get("focus")),
                max_chars=_optional_int(arguments.get("max_chars")),
            )
        if name == "asset_inspect":
            return self.asset_inspect(
                asset_id=str(arguments.get("asset_id") or ""),
                angle=_optional_string(arguments.get("angle")),
            )
        return f"Unknown planner asset tool: {name}"

    def asset_read(
        self,
        *,
        asset_id: str,
        focus: str | None = None,
        max_chars: int | None = None,
    ) -> str:
        result = perform_asset_read(
            surface=self.surface,
            asset_id=asset_id,
            focus=focus,
            max_chars=max_chars,
            api_key=self.api_key,
            cfg=self.cfg,
            model_registry=self.model_registry,
            transport=self.transport,
            client_factory=make_llm_client,
        )
        return result.text

    def asset_inspect(self, *, asset_id: str, angle: str | None = None) -> str:
        count = self._inspect_counts.get(asset_id, 0)
        if count >= _INSPECT_CAP_PER_ASSET:
            return "Inspection cap reached for this asset; proceed with available data or ask the user."
        self._inspect_counts[asset_id] = count + 1
        try:
            handle = self.surface.refresh_comprehension(
                asset_id,
                mode="sync",
                angle=angle,
            )
            record = handle.join()
        except AssetError as exc:
            return f"Asset inspection failed: {exc}"
        if record is None:
            return "Asset inspection did not produce a comprehension record."
        LOGGER.info(
            "asset_inspect asset_id=%s angle=%s result_status=%s",
            asset_id,
            str(bool(angle)).lower(),
            record.status,
        )
        return _comprehension_result(record)


def _comprehension_result(record: ComprehensionRecord) -> str:
    lines = [
        f"Asset inspection complete: {record.asset_id}",
        f"- status: {record.status}",
        f"- source: {record.source}",
        f"- detected_language: {record.detected_language or 'unknown'}",
        f"- summary: {record.data.semantic_summary or '(none)'}",
    ]
    if record.data.stated_facts:
        lines.append("- stated_facts:")
        lines.extend(f"  - {fact}" for fact in record.data.stated_facts[:8])
    if record.data.open_questions:
        lines.append("- open_questions:")
        lines.extend(f"  - {question}" for question in record.data.open_questions[:8])
    return "\n".join(lines)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
