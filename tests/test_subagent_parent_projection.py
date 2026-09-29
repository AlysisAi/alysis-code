from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from alysis_code.agent.subagent_execution import project_subagent_parent_result
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


class _ProjectionClient:
    model = "test-model"
    temperature = 0.0

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(list(kwargs.get("messages") or []))
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-subagent-projection",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "Inspect the projection."},
                    )
                ],
                raw={},
            )
        return LLMResponse(content="Projection verified.", tool_calls=[], raw={})


def test_subagent_run_parent_projection_keeps_protocol_and_drops_diagnostics() -> None:
    raw = {
        "run_id": "child-1",
        "subagent": "explorer",
        "subagent_session_id": "session-1",
        "status": "success",
        "result": "Finding with evidence.",
        "elapsed_ms": 12,
        "steps_completed": 3,
        "effects": ["delegate", "read_workspace"],
        "touched_repo_paths": [],
        "capability_evidence": {"observed_success_event_types": ["final"]},
        "cleanup_pending": True,
        "cleanup_pending_run_ids": ["child-1"],
        "cleanup_warning": "worktree removal must be retried",
        "usage": {"total_tokens": 9000},
        "sandbox": {"mode": "readonly", "tools": ["fs_read"]},
        "deadline": {"source": "subagent_fallback"},
        "report_safety": {"sanitized": False},
        "helper_runs": {"count": 1},
        "provider_trace": {"frames": 100},
    }

    projected = project_subagent_parent_result("subagent_run", raw)

    assert projected == {
        "run_id": "child-1",
        "subagent": "explorer",
        "subagent_session_id": "session-1",
        "status": "success",
        "result": "Finding with evidence.",
        "elapsed_ms": 12,
        "steps_completed": 3,
        "effects": ["delegate", "read_workspace"],
        "touched_repo_paths": [],
        "capability_evidence": {"observed_success_event_types": ["final"]},
        "cleanup_pending": True,
        "cleanup_pending_run_ids": ["child-1"],
        "cleanup_warning": "worktree removal must be retried",
        "report_safety": {"sanitized": False},
    }
    assert raw["usage"] == {"total_tokens": 9000}


def test_subagent_wait_parent_projection_projects_each_completed_child() -> None:
    raw = {
        "results": {
            "child-1": {
                "run_id": "child-1",
                "subagent": "explorer",
                "status": "success",
                "result": "Done.",
                "usage": {"total_tokens": 42},
            }
        },
        "pending_run_ids": ["child-2"],
        "wait_pending": True,
        "summary": "2 children: 1 running, 1 joined",
        "message": "One child is still running.",
        "scheduler_diagnostics": {"workers": 2},
    }

    projected = project_subagent_parent_result("subagent_wait", raw)

    assert projected == {
        "results": {
            "child-1": {
                "run_id": "child-1",
                "subagent": "explorer",
                "status": "success",
                "result": "Done.",
            }
        },
        "pending_run_ids": ["child-2"],
        "wait_pending": True,
        "summary": "2 children: 1 running, 1 joined",
        "message": "One child is still running.",
    }


def test_parent_projection_leaves_non_subagent_tools_unchanged() -> None:
    raw = {"usage": {"total_tokens": 1}, "result": "ordinary tool"}

    assert project_subagent_parent_result("fs_read", raw) is raw


def test_parent_turn_projects_subagent_result_but_retains_full_lifecycle_diagnostics(
    tmp_path: Path,
) -> None:
    session = create_session(
        cfg=AppConfig(model="test-model", stream=False, web_search_mode="off"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="subagent-parent-projection",
        enable_compaction=False,
        enable_tool_output_offload=False,
        enable_conversation_summarization=False,
        verification_enabled=False,
        subagents_enabled=True,
    )
    raw_result = {
        "run_id": "child-1",
        "subagent": "explorer",
        "subagent_session_id": "child-session-1",
        "status": "success",
        "result": "Finding with evidence.",
        "elapsed_ms": 12,
        "steps_completed": 3,
        "effects": ["delegate", "read_workspace"],
        "touched_repo_paths": [],
        "usage": {"total_tokens": 9000},
        "sandbox": {"mode": "readonly", "tools": ["fs_read"]},
        "deadline": {"source": "disabled", "enabled": False},
        "report_safety": {"sanitized": False},
        "helper_runs": {"count": 1},
        "provider_trace": {"frames": 100},
    }
    lifecycle_payload = {
        "name": "explorer",
        **{key: value for key, value in raw_result.items() if key != "subagent"},
    }

    def _run_subagent(_args: dict[str, Any]) -> dict[str, Any]:
        session.store.append("subagent_end", lifecycle_payload)
        return dict(raw_result)

    session.tools["subagent_run"] = replace(
        session.tools["subagent_run"],
        run=_run_subagent,
    )
    client = _ProjectionClient()
    session.client = client  # type: ignore[assignment]
    expected_parent_result = {
        "run_id": "child-1",
        "subagent": "explorer",
        "subagent_session_id": "child-session-1",
        "status": "success",
        "result": "Finding with evidence.",
        "elapsed_ms": 12,
        "steps_completed": 3,
        "effects": ["delegate", "read_workspace"],
        "touched_repo_paths": [],
        "report_safety": {"sanitized": False},
    }

    try:
        assert session.run_turn("Use an explorer and report its finding.") == 0
        log_path = session.store.path
        persisted_tool_message = next(
            message
            for message in reversed(session.messages)
            if message.get("role") == "tool"
            and message.get("tool_call_id") == "call-subagent-projection"
        )
    finally:
        session.close()

    assert len(client.calls) == 2
    model_tool_message = next(
        message
        for message in reversed(client.calls[1])
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "call-subagent-projection"
    )
    assert json.loads(str(model_tool_message["content"])) == expected_parent_result
    assert json.loads(str(persisted_tool_message["content"])) == expected_parent_result

    events = list(read_session_events(log_path))
    persisted_result = next(
        event["payload"]
        for event in events
        if event.get("type") == "tool_result"
        and event.get("payload", {}).get("name") == "subagent_run"
    )
    assert persisted_result["result"] == expected_parent_result
    assert json.loads(str(persisted_result["content"])) == expected_parent_result

    lifecycle = next(event["payload"] for event in events if event.get("type") == "subagent_end")
    assert lifecycle["usage"] == raw_result["usage"]
    assert lifecycle["sandbox"] == raw_result["sandbox"]
    assert lifecycle["deadline"] == raw_result["deadline"]
    assert lifecycle["report_safety"] == raw_result["report_safety"]
    assert lifecycle["helper_runs"] == raw_result["helper_runs"]
    assert lifecycle["provider_trace"] == raw_result["provider_trace"]
