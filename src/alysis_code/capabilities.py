from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilityDefinition:
    """A user-meaningful capability whose readiness is controlled by runtime state."""

    name: str
    description: str
    config_path: tuple[str, ...]
    required_tool_names: tuple[str, ...] = ()
    success_event_types: tuple[str, ...] = ()
    success_tool_names: tuple[str, ...] = ()
    minimum_success_tool_events: int = 0
    materializes_artifacts: bool = False
    disabled_reason: str = "This capability is disabled for this session."
    disabled_resolution: str = "Enable the capability and start a new chat session."
    unavailable_in_mode_reason: str = "This capability is unavailable in the current mode."
    unavailable_in_mode_resolution: str = "Switch to a write-capable mode and retry."
    unavailable_in_mode_requires_new_session: bool = False


@dataclass(frozen=True)
class CapabilityStatus:
    name: str
    available: bool
    reason_code: str | None = None
    reason: str | None = None
    resolution: str | None = None
    requires_new_session: bool = False


_CAPABILITY_DEFINITIONS: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        name="external_research",
        description=("Research dependency and framework behavior with the configured web tools."),
        config_path=("web_tools_enabled",),
        required_tool_names=("web_fetch", "web_search"),
        success_tool_names=("web_fetch", "web_search"),
        minimum_success_tool_events=1,
        disabled_reason="External research is disabled for this session.",
        disabled_resolution=("Enable web tools and web search, then start a new chat session."),
        unavailable_in_mode_reason=(
            "External research is unavailable because this session does not expose "
            "both web_fetch and web_search."
        ),
        unavailable_in_mode_resolution=(
            "Configure web search and enable web tools, then start a new chat session."
        ),
        unavailable_in_mode_requires_new_session=True,
    ),
    CapabilityDefinition(
        name="image_generation",
        description=(
            "Create production raster images and save validated image files in the workspace."
        ),
        config_path=("image_generation", "enabled"),
        required_tool_names=("image_generate",),
        success_event_types=("image_generated",),
        materializes_artifacts=True,
        disabled_reason="Image generation is disabled for this session.",
        disabled_resolution=(
            "Set an image-provider credential, run "
            "`alysis config set image_generation.enabled true`, then start a new chat session."
        ),
        unavailable_in_mode_reason=(
            "Image generation is not available in the current session mode."
        ),
        unavailable_in_mode_resolution=(
            "Switch to `review`, `auto`, or `fullaccess` mode and retry."
        ),
    ),
)

_CAPABILITY_BY_NAME = {definition.name: definition for definition in _CAPABILITY_DEFINITIONS}


def iter_capability_definitions() -> Iterator[CapabilityDefinition]:
    return iter(_CAPABILITY_DEFINITIONS)


def get_capability_definition(name: str) -> CapabilityDefinition | None:
    return _CAPABILITY_BY_NAME.get(str(name or "").strip().casefold())


def _config_flag(cfg: Any, path: tuple[str, ...]) -> bool:
    current = cfg
    for segment in path:
        if current is None:
            return False
        current = getattr(current, segment, None)
    return bool(current)


def resolve_capability_status(
    name: str,
    *,
    cfg: Any,
    available_tool_names: Collection[str] | None = None,
) -> CapabilityStatus:
    definition = get_capability_definition(name)
    clean_name = str(name or "").strip().casefold()
    if definition is None:
        return CapabilityStatus(
            name=clean_name,
            available=False,
            reason_code="unknown_capability",
            reason=f"Unknown capability: {clean_name or '(empty)'}.",
        )

    if not _config_flag(cfg, definition.config_path):
        return CapabilityStatus(
            name=definition.name,
            available=False,
            reason_code="capability_disabled",
            reason=definition.disabled_reason,
            resolution=definition.disabled_resolution,
            requires_new_session=True,
        )

    if available_tool_names is not None and definition.required_tool_names:
        available = {str(item or "").strip() for item in available_tool_names}
        if any(tool_name not in available for tool_name in definition.required_tool_names):
            return CapabilityStatus(
                name=definition.name,
                available=False,
                reason_code="capability_unavailable_in_mode",
                reason=definition.unavailable_in_mode_reason,
                resolution=definition.unavailable_in_mode_resolution,
                requires_new_session=definition.unavailable_in_mode_requires_new_session,
            )

    return CapabilityStatus(name=definition.name, available=True)
