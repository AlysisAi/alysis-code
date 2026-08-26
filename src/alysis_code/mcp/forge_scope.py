from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import ConfigError
from .models import normalize_server_id


@dataclass(frozen=True)
class ForgeAllowedMcpTool:
    server_id: str
    tool_name: str

    def as_payload(self) -> dict[str, str]:
        return {
            "server_id": self.server_id,
            "tool_name": self.tool_name,
        }


@dataclass(frozen=True)
class ForgeTaskMcpScope:
    allow_resources: bool = False
    allowed_tools: tuple[ForgeAllowedMcpTool, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.allow_resources and not self.allowed_tools

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.allow_resources:
            payload["allow_resources"] = True
        if self.allowed_tools:
            payload["allowed_tools"] = [item.as_payload() for item in self.allowed_tools]
        return payload


def serialize_task_mcp_scope(scope: ForgeTaskMcpScope | None) -> dict[str, Any] | None:
    if scope is None or scope.is_empty:
        return None
    return scope.as_payload()


def describe_task_mcp_scope(scope: ForgeTaskMcpScope | None) -> str:
    if scope is None or scope.is_empty:
        return "disabled (default deny-by-default)"
    parts = [
        (
            "generic MCP resources allowed"
            if scope.allow_resources
            else "generic MCP resources blocked"
        ),
        (
            "allowed live MCP tools: "
            + ", ".join(f"{item.server_id}/{item.tool_name}" for item in scope.allowed_tools)
            if scope.allowed_tools
            else "allowed live MCP tools: (none)"
        ),
    ]
    return "; ".join(parts)


def normalize_task_mcp_scope(
    value: Any,
    *,
    warning_prefix: str,
) -> tuple[ForgeTaskMcpScope | None, list[str]]:
    if value is None:
        return None, []

    warnings: list[str] = []
    if not isinstance(value, dict):
        return None, [f"{warning_prefix}: dropped invalid mcp_scope: expected an object."]

    allow_resources = False
    if "allow_resources" in value:
        raw_allow_resources = value.get("allow_resources")
        if isinstance(raw_allow_resources, bool):
            allow_resources = raw_allow_resources
        else:
            warnings.append(
                f"{warning_prefix}: dropped invalid mcp_scope.allow_resources value; expected true/false."
            )

    allowed_tools: list[ForgeAllowedMcpTool] = []
    seen_tool_keys: set[tuple[str, str]] = set()
    if "allowed_tools" in value:
        raw_allowed_tools = value.get("allowed_tools")
        if not isinstance(raw_allowed_tools, list):
            warnings.append(
                f"{warning_prefix}: dropped invalid mcp_scope.allowed_tools value; expected an array."
            )
        else:
            for index, raw_item in enumerate(raw_allowed_tools):
                normalized_tool, tool_warnings = _normalize_allowed_tool_entry(
                    raw_item,
                    warning_prefix=warning_prefix,
                    index=index,
                )
                warnings.extend(tool_warnings)
                if normalized_tool is None:
                    continue
                dedupe_key = (
                    normalized_tool.server_id,
                    normalized_tool.tool_name.casefold(),
                )
                if dedupe_key in seen_tool_keys:
                    continue
                seen_tool_keys.add(dedupe_key)
                allowed_tools.append(normalized_tool)

    scope = ForgeTaskMcpScope(
        allow_resources=allow_resources,
        allowed_tools=tuple(allowed_tools),
    )
    if scope.is_empty:
        warnings.append(
            f"{warning_prefix}: normalized empty mcp_scope; MCP execution remains deny-by-default."
        )
        return None, warnings
    return scope, warnings


def _normalize_allowed_tool_entry(
    raw_item: Any,
    *,
    warning_prefix: str,
    index: int,
) -> tuple[ForgeAllowedMcpTool | None, list[str]]:
    item_label = f"{warning_prefix}: dropped invalid mcp_scope.allowed_tools[{index}] entry"
    if not isinstance(raw_item, dict):
        return None, [f"{item_label}: expected an object."]

    raw_server_id = raw_item.get("server_id")
    if not isinstance(raw_server_id, str):
        return None, [f"{item_label}: server_id must be a string."]
    server_id_value = raw_server_id.strip()
    if not server_id_value:
        return None, [f"{item_label}: server_id cannot be empty."]
    try:
        server_id = normalize_server_id(server_id_value)
    except ConfigError as exc:
        return None, [f"{item_label}: {exc}"]

    raw_tool_name = raw_item.get("tool_name")
    if not isinstance(raw_tool_name, str):
        return None, [f"{item_label}: tool_name must be a string."]
    tool_name = raw_tool_name.strip()
    if not tool_name:
        return None, [f"{item_label}: tool_name cannot be empty."]

    return ForgeAllowedMcpTool(server_id=server_id, tool_name=tool_name), []
