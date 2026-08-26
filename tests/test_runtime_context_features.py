from __future__ import annotations

from alysis_code.compaction.settings import CompactionSettings
from alysis_code.runtime_context_features import resolve_runtime_context_features


def test_legacy_enable_compaction_preserves_previous_behavior() -> None:
    settings = CompactionSettings(
        enabled=True, offload_tool_outputs=True, summarize_conversation=True
    )

    enabled = resolve_runtime_context_features(
        settings=settings,
        enable_compaction=True,
    )
    disabled = resolve_runtime_context_features(
        settings=settings,
        enable_compaction=False,
    )

    assert enabled.tool_output_offload_enabled is True
    assert enabled.conversation_summarization_enabled is True
    assert disabled.tool_output_offload_enabled is False
    assert disabled.conversation_summarization_enabled is False


def test_explicit_flags_override_legacy_umbrella() -> None:
    settings = CompactionSettings(
        enabled=True, offload_tool_outputs=True, summarize_conversation=True
    )

    resolved = resolve_runtime_context_features(
        settings=settings,
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
    )

    assert resolved.requested_enable_compaction is False
    assert resolved.requested_tool_output_offload is True
    assert resolved.requested_conversation_summarization is False
    assert resolved.tool_output_offload_enabled is True
    assert resolved.conversation_summarization_enabled is False
    assert resolved.any_enabled is True


def test_settings_gate_effective_runtime_context_features() -> None:
    settings = CompactionSettings(
        enabled=True,
        offload_tool_outputs=False,
        summarize_conversation=False,
    )

    resolved = resolve_runtime_context_features(
        settings=settings,
        enable_compaction=True,
        enable_tool_output_offload=True,
        enable_conversation_summarization=True,
    )

    assert resolved.requested_tool_output_offload is True
    assert resolved.requested_conversation_summarization is True
    assert resolved.tool_output_offload_enabled is False
    assert resolved.conversation_summarization_enabled is False
    assert resolved.any_enabled is False


def test_logging_disabled_without_explicit_artifact_root_disables_offload() -> None:
    settings = CompactionSettings(
        enabled=True, offload_tool_outputs=True, summarize_conversation=False
    )

    resolved = resolve_runtime_context_features(
        settings=settings,
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
        logging_enabled=False,
        explicit_session_artifact_root=False,
    )

    assert resolved.logging_enabled is False
    assert resolved.explicit_session_artifact_root is False
    assert resolved.tool_output_offload_artifact_persistence_available is False
    assert resolved.tool_output_offload_enabled is False


def test_explicit_artifact_root_keeps_offload_enabled_without_logging() -> None:
    settings = CompactionSettings(
        enabled=True, offload_tool_outputs=True, summarize_conversation=False
    )

    resolved = resolve_runtime_context_features(
        settings=settings,
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
        logging_enabled=False,
        explicit_session_artifact_root=True,
    )

    assert resolved.logging_enabled is False
    assert resolved.explicit_session_artifact_root is True
    assert resolved.tool_output_offload_artifact_persistence_available is True
    assert resolved.tool_output_offload_enabled is True
