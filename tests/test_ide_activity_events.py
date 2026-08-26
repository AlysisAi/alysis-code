from __future__ import annotations

import json

import pytest

from alysis_code.ide import activity_events
from alysis_code.ide.activity_events import (
    activity_capabilities,
    diff_stats,
    patch_activity,
    semantic_tool_name,
    tool_activity,
)
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.surface.types import PatchEvent, ToolEndEvent, ToolStartEvent


def test_activity_capabilities_publish_semantic_contract() -> None:
    capabilities = activity_capabilities()

    assert capabilities["event_type"] == "activity_update"
    assert "edit" in capabilities["kinds"]
    assert "succeeded" in capabilities["statuses"]
    assert capabilities["patch_payload"] is True
    assert capabilities["legacy_tool_events_preserved"] is True


def test_internal_tool_names_become_stable_friendly_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOOL_API_KEY", "activity-secret-value")

    activity = tool_activity(
        call_id="call-1",
        name="rs.read",
        arguments={"path": "src/main.py", "api_key": "activity-secret-value"},
        status="running",
        metadata={"source": "runtime", "internal_tool_name": "rs.read"},
    ).to_payload()

    rendered = json.dumps(activity, sort_keys=True)
    assert semantic_tool_name("rs.read") == "fs_read"
    assert activity["kind"] == "read"
    assert activity["operation"] == "read_file"
    assert activity["display_title"] == "Read file"
    assert activity["target"] == "src/main.py"
    assert "rs.read" not in rendered
    assert "activity-secret-value" not in rendered
    assert "internal_tool_name" not in activity["metadata"]


def test_external_and_unknown_tool_names_never_leak_private_names() -> None:
    external = tool_activity(call_id="1", name="mcp__server__private_search").to_payload()
    internal = tool_activity(call_id="2", name="internal.runtime.helper").to_payload()

    assert external["display_title"] == "Use connected tool"
    assert external["operation"] == "use_external_tool"
    assert "mcp__" not in json.dumps(external)
    assert internal["display_title"] == "Use developer tool"
    assert "internal.runtime.helper" not in json.dumps(internal)


def test_patch_activity_preserves_safe_diff_and_exact_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCH_SECRET", "patch-secret-value")
    event = PatchEvent(
        files=["a.py", "b.py"],
        diff=(
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "-old\n"
            "+new\n"
            "+token=patch-secret-value\n"
        ),
        summary="Updated token=patch-secret-value",
    )

    payload = patch_activity(event).to_payload()

    assert payload["operation"] == "prepare_workspace_patch"
    assert payload["display_title"] == "Prepared workspace changes"
    assert payload["files"] == ["a.py", "b.py"]
    assert payload["diff"] == {"files": 2, "additions": 2, "deletions": 1}
    assert "+new" in payload["patch"]
    assert "patch-secret-value" not in json.dumps(payload)
    assert payload["activity_id"].startswith("patch-")


def test_patch_size_is_bounded_without_losing_diff_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(activity_events, "MAX_PATCH_BYTES", 128)
    payload = patch_activity(PatchEvent(files=["a.py"], diff="+x\n" * 200)).to_payload()

    assert len(payload["patch"].encode("utf-8")) <= 128
    assert payload["patch_truncated"] is True
    assert payload["metadata"]["truncated"] is True
    assert payload["diff"]["files"] == 1


def test_diff_stats_do_not_count_file_headers() -> None:
    stats = diff_stats("--- a/file\n+++ b/file\n-old\n+new\n", file_count=1)
    assert stats.to_dict() == {"files": 1, "additions": 1, "deletions": 1}


def test_protocol_surface_emits_patch_as_structured_activity() -> None:
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
    )

    surface.on_patch_generated(
        PatchEvent(files=["a.py"], diff="--- a/a.py\n+++ b/a.py\n+new\n", summary="Add line")
    )

    assert [event["type"] for event in emitted] == ["activity_update"]
    payload = emitted[0]["payload"]
    assert payload["display_title"] == "Prepared workspace changes"
    assert payload["patch"].endswith("+new\n")
    assert payload["diff"] == {"files": 1, "additions": 1, "deletions": 0}


def test_protocol_surface_semantic_tool_events_are_opt_in_and_legacy_names_are_safe() -> None:
    emitted: list[dict] = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session-1"),
        emit=emitted.append,
        semantic_activity_events=True,
    )

    surface.on_tool_start(
        ToolStartEvent(
            tool_call_id="call-1",
            name="rs.read",
            args={"path": "README.md"},
            step=1,
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="call-1",
            name="rs.read",
            status="done",
            elapsed_ms=9,
            meta={"output_bytes": 42, "raw_internal_id": "rs.read"},
        )
    )

    assert [event["type"] for event in emitted] == [
        "tool_call_started",
        "activity_update",
        "tool_call_completed",
        "activity_update",
    ]
    assert emitted[0]["payload"]["name"] == "fs_read"
    started = emitted[1]["payload"]
    completed = emitted[3]["payload"]
    assert started["display_title"] == "Read file"
    assert started["target"] == "README.md"
    assert completed["status"] == "succeeded"
    assert completed["duration_ms"] == 9
    assert completed["metadata"] == {"output_bytes": 42, "worker_id": None}
    assert "rs.read" not in json.dumps(emitted)
