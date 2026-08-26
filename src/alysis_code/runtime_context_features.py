from __future__ import annotations

from dataclasses import dataclass

from .compaction.settings import CompactionSettings


@dataclass(frozen=True)
class RuntimeContextFeatures:
    requested_enable_compaction: bool
    requested_tool_output_offload: bool
    requested_conversation_summarization: bool
    tool_output_offload_enabled: bool
    conversation_summarization_enabled: bool
    logging_enabled: bool
    explicit_session_artifact_root: bool
    tool_output_offload_artifact_persistence_available: bool
    settings_enabled: bool
    settings_offload_tool_outputs: bool
    settings_summarize_conversation: bool

    @property
    def any_enabled(self) -> bool:
        return self.tool_output_offload_enabled or self.conversation_summarization_enabled


def resolve_runtime_context_features(
    *,
    settings: CompactionSettings,
    enable_compaction: bool,
    enable_tool_output_offload: bool | None = None,
    enable_conversation_summarization: bool | None = None,
    logging_enabled: bool = True,
    explicit_session_artifact_root: bool = False,
) -> RuntimeContextFeatures:
    requested_enable_compaction = bool(enable_compaction)
    requested_tool_output_offload = (
        requested_enable_compaction
        if enable_tool_output_offload is None
        else bool(enable_tool_output_offload)
    )
    requested_conversation_summarization = (
        requested_enable_compaction
        if enable_conversation_summarization is None
        else bool(enable_conversation_summarization)
    )

    settings_enabled = bool(settings.enabled)
    settings_offload_tool_outputs = bool(settings.offload_tool_outputs)
    settings_summarize_conversation = bool(settings.summarize_conversation)
    tool_output_offload_artifact_persistence_available = bool(
        logging_enabled or explicit_session_artifact_root
    )

    return RuntimeContextFeatures(
        requested_enable_compaction=requested_enable_compaction,
        requested_tool_output_offload=requested_tool_output_offload,
        requested_conversation_summarization=requested_conversation_summarization,
        tool_output_offload_enabled=(
            requested_tool_output_offload
            and settings_enabled
            and settings_offload_tool_outputs
            and tool_output_offload_artifact_persistence_available
        ),
        conversation_summarization_enabled=(
            requested_conversation_summarization
            and settings_enabled
            and settings_summarize_conversation
        ),
        logging_enabled=bool(logging_enabled),
        explicit_session_artifact_root=bool(explicit_session_artifact_root),
        tool_output_offload_artifact_persistence_available=(
            tool_output_offload_artifact_persistence_available
        ),
        settings_enabled=settings_enabled,
        settings_offload_tool_outputs=settings_offload_tool_outputs,
        settings_summarize_conversation=settings_summarize_conversation,
    )
