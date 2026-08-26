from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..mcp.manager import McpHostToolBinding
from ..model_registry import ModelRegistry
from .asset_read_core import perform_asset_read
from .planner_tools import ASSET_READ_TOOL
from .surface import AssetSurface
from .untrusted_content import build_untrusted_asset_text_block
from .usage_logger import AssetUsageLogger
from .worker_mirror import MirroredAssetEntry, TaskAssetMirror, load_task_asset_mirror

LOGGER = logging.getLogger(__name__)

ASSET_LOAD_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "asset_load",
        "description": (
            "Load a may-need or pinned asset's full content into the working context. "
            "For primary assets already inlined, use asset_read."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_id"],
            "properties": {
                "asset_id": {"type": "string"},
            },
        },
    },
}


class WorkerAssetToolRunner:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        surface: AssetSurface,
        model_registry: ModelRegistry,
        mirror: TaskAssetMirror,
        usage_logger: AssetUsageLogger | None = None,
        api_key: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.surface = surface
        self.model_registry = model_registry
        self.mirror = mirror
        self.usage_logger = usage_logger
        self.api_key = api_key
        self.loaded_image_asset_ids: set[str] = set()

    def asset_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(arguments.get("asset_id") or "").strip()
        focus = _optional_string(arguments.get("focus"))
        max_chars = _optional_int(arguments.get("max_chars"))
        entry = _find_entry(self.mirror, asset_id)
        if entry is not None and focus is None:
            text = _read_from_mirror_entry(entry, max_chars=max_chars or 8000)
            if self.usage_logger is not None:
                self.usage_logger.asset_read(
                    asset_id=asset_id,
                    focus=False,
                    chars=len(text),
                    cached=False,
                )
            return {
                "asset_id": asset_id,
                "text": text,
                "chars": len(text),
                "cached": False,
                "truncated": "...[truncated to asset block limit]..." in text,
            }
        result = perform_asset_read(
            surface=self.surface,
            asset_id=asset_id,
            focus=focus,
            max_chars=max_chars,
            api_key=self.api_key,
            cfg=self.cfg,
            model_registry=self.model_registry,
        )
        if self.usage_logger is not None:
            self.usage_logger.asset_read(
                asset_id=asset_id,
                focus=bool(focus),
                chars=result.chars,
                cached=result.cached,
            )
        return {
            "asset_id": asset_id,
            "text": result.text,
            "chars": result.chars,
            "cached": result.cached,
            "truncated": result.truncated,
        }

    def asset_load(self, arguments: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(arguments.get("asset_id") or "").strip()
        entry = _find_entry(self.mirror, asset_id)
        if entry is None:
            text = f"Asset {asset_id} is unavailable in this task workspace."
            self._record_asset_load(asset_id=asset_id, kind="unknown", chars=len(text))
            return {"asset_id": asset_id, "text": text, "chars": len(text), "available": False}
        if entry.status == "deleted":
            text = f'Asset {entry.asset_id} - "{entry.title}" has been deleted and is unavailable.'
            self._record_asset_load(asset_id=entry.asset_id, kind=entry.kind, chars=len(text))
            return {"asset_id": asset_id, "text": text, "chars": len(text), "available": False}
        if entry.status == "missing":
            text = f'Asset {entry.asset_id} - "{entry.title}" is missing from the mirror.'
            self._record_asset_load(asset_id=entry.asset_id, kind=entry.kind, chars=len(text))
            return {"asset_id": asset_id, "text": text, "chars": len(text), "available": False}
        content = _load_entry_text(
            entry,
            max_chars=int(self.cfg.assets.worker.max_chars_per_asset_block),
        )
        text = _tool_result_text(
            entry=entry,
            content=content,
            source="mirror_load",
            truncated="...[truncated to asset block limit]..." in content,
        )
        if entry.kind == "image":
            self.loaded_image_asset_ids.add(entry.asset_id)
        self._record_asset_load(asset_id=entry.asset_id, kind=entry.kind, chars=len(text))
        return {
            "asset_id": entry.asset_id,
            "kind": entry.kind,
            "text": text,
            "chars": len(text),
            "available": True,
        }

    def _record_asset_load(self, *, asset_id: str, kind: str, chars: int) -> None:
        if self.usage_logger is not None:
            self.usage_logger.asset_load(
                asset_id=asset_id,
                kind=kind,
                chars=chars,
            )
        LOGGER.info(
            "asset_load task_id=%s asset_id=%s kind=%s chars=%s",
            self.mirror.task_id or self.mirror.manifest_path.stem,
            asset_id,
            kind,
            chars,
        )


class AssetWorkerMcpManager:
    def __init__(self, *, runner: WorkerAssetToolRunner) -> None:
        self.runner = runner
        self.resolved_config = _AssetToolResolvedConfig()
        self._closed = False
        asset_read = copy.deepcopy(ASSET_READ_TOOL["function"])
        asset_load = copy.deepcopy(ASSET_LOAD_TOOL["function"])
        self._bindings = (
            McpHostToolBinding(
                tool_name="asset_read",
                tool_alias="asset_read",
                description=str(asset_read["description"]),
                parameters=dict(asset_read["parameters"]),
                run_handler=self.runner.asset_read,
            ),
            McpHostToolBinding(
                tool_name="asset_load",
                tool_alias="asset_load",
                description=str(asset_load["description"]),
                parameters=dict(asset_load["parameters"]),
                run_handler=self.runner.asset_load,
            ),
        )

    @property
    def tool_bindings(self) -> tuple[McpHostToolBinding, ...]:
        return self._bindings

    @property
    def closed(self) -> bool:
        return self._closed

    def startup_metadata(self) -> dict[str, Any]:
        return {
            "config_present": True,
            "user_config_present": False,
            "project_config_present": False,
            "resolved_server_count": 0,
            "resolved_server_ids": [],
            "active_server_count": 0,
            "active_server_ids": [],
            "live_tool_runtime_enabled": False,
            "asset_worker_tools_enabled": True,
        }

    def catalog_snapshot_metadata(self) -> dict[str, Any]:
        return {
            "catalog_initialized": True,
            "live_tool_runtime_enabled": False,
            "active_server_ids": [],
            "active_server_count": 0,
            "server_catalogs": [],
            "exposed_tool_aliases": [binding.tool_alias for binding in self._bindings],
            "exposed_tool_names": [binding.tool_name for binding in self._bindings],
            "exposed_tool_count": len(self._bindings),
            "snapshotted_resource_count": 0,
            "resource_tool_names": [],
            "resource_tool_count": 0,
            "asset_worker_tools_enabled": True,
        }

    def execution_context_summary(self) -> dict[str, Any]:
        return {
            "active_server_ids": [],
            "servers": [],
            "asset_worker_tools": [binding.tool_alias for binding in self._bindings],
        }

    def close(self) -> None:
        self._closed = True


class CompositeWorkerMcpManager:
    def __init__(
        self,
        *,
        base_manager: Any | None,
        asset_manager: AssetWorkerMcpManager,
    ) -> None:
        self.base_manager = base_manager
        self.asset_manager = asset_manager
        self.resolved_config = _AssetToolResolvedConfig(has_any_config=True)

    @property
    def tool_bindings(self) -> tuple[Any, ...]:
        base_bindings = tuple(getattr(self.base_manager, "tool_bindings", ()) or ())
        asset_bindings = tuple(self.asset_manager.tool_bindings)
        asset_aliases = {str(getattr(binding, "tool_alias", "")) for binding in asset_bindings}
        return (
            *(
                binding
                for binding in base_bindings
                if str(getattr(binding, "tool_alias", "")) not in asset_aliases
            ),
            *asset_bindings,
        )

    @property
    def closed(self) -> bool:
        base_closed = (
            True if self.base_manager is None else bool(getattr(self.base_manager, "closed", False))
        )
        return base_closed and self.asset_manager.closed

    def startup_metadata(self) -> dict[str, Any]:
        if self.base_manager is None:
            payload = self.asset_manager.startup_metadata()
        else:
            payload = copy.deepcopy(self.base_manager.startup_metadata())
        payload["asset_worker_tools_enabled"] = True
        payload["asset_worker_tool_count"] = len(self.asset_manager.tool_bindings)
        return payload

    def catalog_snapshot_metadata(self) -> dict[str, Any]:
        if self.base_manager is None:
            payload = self.asset_manager.catalog_snapshot_metadata()
        else:
            payload = copy.deepcopy(self.base_manager.catalog_snapshot_metadata())
        asset_payload = self.asset_manager.catalog_snapshot_metadata()
        aliases = _append_unique_strings(
            payload.get("exposed_tool_aliases"),
            asset_payload.get("exposed_tool_aliases"),
        )
        names = _append_unique_strings(
            payload.get("exposed_tool_names"),
            asset_payload.get("exposed_tool_names"),
        )
        payload["exposed_tool_aliases"] = aliases
        payload["exposed_tool_names"] = names
        payload["exposed_tool_count"] = len(aliases)
        payload["asset_worker_tools_enabled"] = True
        payload["asset_worker_tool_count"] = len(asset_payload.get("exposed_tool_aliases") or [])
        return payload

    def execution_context_summary(self) -> dict[str, Any]:
        if self.base_manager is None:
            payload = self.asset_manager.execution_context_summary()
        else:
            payload = copy.deepcopy(self.base_manager.execution_context_summary())
        payload["asset_worker_tools"] = [
            binding.tool_alias for binding in self.asset_manager.tool_bindings
        ]
        return payload

    def close(self) -> None:
        try:
            self.asset_manager.close()
        finally:
            if self.base_manager is not None:
                self.base_manager.close()


@dataclass(frozen=True)
class _AssetToolResolvedConfig:
    has_any_config: bool = True


def build_worker_asset_mcp_manager(
    *,
    cfg: AppConfig,
    surface: AssetSurface,
    model_registry: ModelRegistry,
    mirror: TaskAssetMirror,
    usage_logger: AssetUsageLogger | None = None,
    api_key: str | None = None,
) -> AssetWorkerMcpManager:
    return AssetWorkerMcpManager(
        runner=WorkerAssetToolRunner(
            cfg=cfg,
            surface=surface,
            model_registry=model_registry,
            mirror=mirror,
            usage_logger=usage_logger,
            api_key=api_key,
        )
    )


def load_worker_asset_mcp_manager(
    *,
    cfg: AppConfig,
    surface: AssetSurface,
    model_registry: ModelRegistry,
    workspace_path: Path,
    usage_logger: AssetUsageLogger | None = None,
    api_key: str | None = None,
) -> AssetWorkerMcpManager:
    return build_worker_asset_mcp_manager(
        cfg=cfg,
        surface=surface,
        model_registry=model_registry,
        mirror=load_task_asset_mirror(workspace_path=workspace_path),
        usage_logger=usage_logger,
        api_key=api_key,
    )


def _find_entry(mirror: TaskAssetMirror, asset_id: str) -> MirroredAssetEntry | None:
    for entry in [*mirror.primary, *mirror.may_need, *mirror.pinned]:
        if entry.asset_id == asset_id:
            return entry
    return None


def compose_worker_asset_mcp_manager(
    *,
    base_manager: Any | None,
    asset_manager: AssetWorkerMcpManager,
) -> CompositeWorkerMcpManager:
    return CompositeWorkerMcpManager(base_manager=base_manager, asset_manager=asset_manager)


def _load_entry_text(entry: MirroredAssetEntry, *, max_chars: int) -> str:
    if entry.kind == "text" and entry.extracted_text_workspace_path is not None:
        text = _read_text(entry.extracted_text_workspace_path)
    elif entry.kind == "text" and entry.raw_workspace_path is not None:
        text = _read_text(entry.raw_workspace_path)
    else:
        text = _image_entry_text(entry)
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n\n...[truncated to asset block limit]..."
    return text


def _read_from_mirror_entry(entry: MirroredAssetEntry, *, max_chars: int) -> str:
    if entry.status == "deleted":
        return f'Asset {entry.asset_id} - "{entry.title}" has been deleted and is unavailable.'
    if entry.status == "missing":
        return f'Asset {entry.asset_id} - "{entry.title}" is missing from the mirror.'
    content = _load_entry_text(entry, max_chars=max_chars)
    return _tool_result_text(
        entry=entry,
        content=content,
        source="mirror_read",
        truncated="...[truncated to asset block limit]..." in content,
    )


def _image_entry_text(entry: MirroredAssetEntry) -> str:
    parts: list[str] = []
    if entry.comprehension is not None:
        comp = entry.comprehension
        parts.append(f"Comprehension summary:\n{comp.data.semantic_summary}")
        if comp.data.key_entities:
            entities = [
                f"{item.get('type')}={item.get('value')}"
                for item in comp.data.key_entities
                if isinstance(item, dict) and item.get("type") and item.get("value")
            ]
            if entities:
                parts.append("Key entities:\n" + ", ".join(entities))
    if entry.extracted_text_workspace_path is not None:
        ocr_text = _read_text(entry.extracted_text_workspace_path)
        if ocr_text:
            parts.append("OCR text:\n" + ocr_text)
    return "\n\n".join(part for part in parts if part.strip()) or "(no readable image content)"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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


def _tool_result_text(
    *,
    entry: MirroredAssetEntry,
    content: str,
    source: str,
    truncated: bool,
) -> str:
    header = (
        f'Asset {entry.asset_id} - "{entry.title}" '
        f"({entry.kind}, mirrored workspace copy, source={source})"
    )
    framed = build_untrusted_asset_text_block(
        asset_id=entry.asset_id,
        text=content,
        mime_type=entry.mime,
        original_char_count=len(content),
        truncated=truncated,
    )
    return f"{header}\n\nContent:\n{framed}".rstrip()


def _append_unique_strings(left: Any, right: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in [*(left or []), *(right or [])]:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out
