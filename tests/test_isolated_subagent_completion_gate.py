from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from alysis_code.agent_loop import ToolDef, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.subagents import SubagentDefinition


class _SingleCandidateClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="candidate-run",
                        name="subagent_run",
                        arguments={
                            "name": "implementer",
                            "task": "Create the requested candidate change.",
                            "mode": "auto",
                            "workspace_view": "isolated",
                        },
                    )
                ],
                raw={},
            )
        if self.calls == 2:
            return LLMResponse(
                content="The isolated candidate was retained successfully.",
                tool_calls=[],
                raw={},
            )
        raise AssertionError("completion gating replayed after a retained material candidate")


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _run_candidate_turn(
    *,
    tmp_path: Path,
    session_id: str,
    tool_result: dict[str, Any],
    unapplied_results: list[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]], Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "candidate.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "Alysis Test"],
        check=True,
    )
    subprocess.run(["git", "-C", repo, "add", "candidate.txt"], check=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-qm", "baseline"],
        check=True,
    )
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="auto"),
        root=repo,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
        subagents_enabled=True,
        subagent_registry={
            "implementer": SubagentDefinition(
                name="implementer",
                description="Implement a scoped candidate.",
                system_prompt="Implement the requested candidate.",
                mode="auto",
            )
        },
    )
    original = session.tools["subagent_run"]
    tool_calls: list[dict[str, Any]] = []

    def _candidate_result(arguments: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(dict(arguments))
        return dict(tool_result)

    session.tools["subagent_run"] = ToolDef(
        name=original.name,
        description=original.description,
        parameters=original.parameters,
        run=_candidate_result,
        metadata=original.metadata,
    )
    scheduler = session.child_scheduler
    assert scheduler is not None
    scheduler.unapplied_isolated_results = (  # type: ignore[method-assign]
        lambda: list(unapplied_results)
    )
    client = _SingleCandidateClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Produce one isolated implementation lifecycle result.")
        log_path = session.store.path
    finally:
        session.close()

    return exit_code, client.calls, tool_calls, log_path


def test_interactive_final_accepts_one_retained_isolated_material_candidate(
    tmp_path: Path,
) -> None:
    exit_code, client_calls, tool_calls, log_path = _run_candidate_turn(
        tmp_path=tmp_path,
        session_id="retained-isolated-completion",
        tool_result={
            "run_id": "retained-run",
            "subagent": "implementer",
            "subagent_session_id": "retained-child-session",
            "status": "success",
            "result": "candidate.txt was changed in the isolated workspace.",
            "effects": ["delegate", "write_workspace"],
            "touched_repo_paths": ["candidate.txt"],
            "material_touched_repo_paths": ["candidate.txt"],
            "workspace": {
                "view": "isolated",
                "base_commit": "base-commit",
                "state": "captured",
                "parent_dirty_paths": [],
                "no_changes": False,
            },
            "patch_summary": {
                "files": ["candidate.txt"],
                "insertions": 1,
                "deletions": 0,
                "sha256": "a" * 64,
            },
        },
        unapplied_results=[
            {
                "run_id": "retained-run",
                "files": ["candidate.txt"],
                "insertions": 1,
                "deletions": 0,
            }
        ],
    )

    assert exit_code == 0
    assert client_calls == 2
    assert len(tool_calls) == 1
    assert _event_payloads(log_path, "completion_gate_nudge") == []
    assert _event_payloads(log_path, "interactive_no_material_edits_detected") == []
    finalized = _event_payloads(log_path, "turn_intent_finalized")
    assert finalized[-1]["state"]["material_edit_count"] == 0
    assert finalized[-1]["turn_retained_isolated_material_run_ids"] == ["retained-run"]
    assert finalized[-1]["state"]["completion_certificate"]["problems"] == []


def test_interactive_final_accepts_structured_duplicate_lifecycle_result(
    tmp_path: Path,
) -> None:
    exit_code, client_calls, tool_calls, log_path = _run_candidate_turn(
        tmp_path=tmp_path,
        session_id="duplicate-isolated-completion",
        tool_result={
            "run_id": "duplicate-run",
            "subagent": "implementer",
            "subagent_session_id": "duplicate-child-session",
            "status": "success",
            "result": "The candidate duplicates a retained canonical result.",
            "effects": ["delegate", "write_workspace"],
            "touched_repo_paths": ["candidate.txt"],
            "material_touched_repo_paths": ["candidate.txt"],
            "semantic_no_progress": True,
            "duplicate_of": "canonical-run",
            "canonical_run_id": "canonical-run",
            "canonical_state": "captured",
            "candidate_worktree_retained": False,
            "physical_worktree_removed": True,
            "workspace": {
                "view": "isolated",
                "base_commit": "base-commit",
                "state": "duplicate",
                "parent_dirty_paths": [],
                "no_changes": False,
            },
            "patch_summary": {
                "files": ["candidate.txt"],
                "insertions": 1,
                "deletions": 0,
                "sha256": "a" * 64,
            },
        },
        unapplied_results=[
            {
                "run_id": "canonical-run",
                "files": ["candidate.txt"],
                "insertions": 1,
                "deletions": 0,
            }
        ],
    )

    assert exit_code == 0
    assert client_calls == 2
    assert len(tool_calls) == 1
    assert _event_payloads(log_path, "completion_gate_nudge") == []
    assert _event_payloads(log_path, "interactive_no_material_edits_detected") == []
    finalized = _event_payloads(log_path, "turn_intent_finalized")
    assert finalized[-1]["state"]["material_edit_count"] == 0
    assert finalized[-1]["turn_retained_isolated_material_run_ids"] == []
    assert finalized[-1]["state"]["completion_certificate"]["problems"] == []


def test_interactive_final_accepts_structured_no_change_lifecycle_result(
    tmp_path: Path,
) -> None:
    exit_code, client_calls, tool_calls, log_path = _run_candidate_turn(
        tmp_path=tmp_path,
        session_id="no-change-isolated-completion",
        tool_result={
            "run_id": "no-change-run",
            "subagent": "implementer",
            "subagent_session_id": "no-change-child-session",
            "status": "success",
            "result": "The isolated child made no repository changes.",
            "effects": ["delegate"],
            "touched_repo_paths": [],
            "material_touched_repo_paths": [],
            "semantic_no_progress": True,
            "candidate_worktree_retained": False,
            "physical_worktree_removed": True,
            "workspace": {
                "view": "isolated",
                "base_commit": "base-commit",
                "state": "discarded",
                "parent_dirty_paths": [],
                "no_changes": True,
            },
            "patch_summary": {
                "files": [],
                "insertions": 0,
                "deletions": 0,
                "sha256": "",
            },
        },
        unapplied_results=[],
    )

    assert exit_code == 0
    assert client_calls == 2
    assert len(tool_calls) == 1
    assert _event_payloads(log_path, "completion_gate_nudge") == []
    assert _event_payloads(log_path, "interactive_no_material_edits_detected") == []
    finalized = _event_payloads(log_path, "turn_intent_finalized")
    assert finalized[-1]["state"]["material_edit_count"] == 0
    assert finalized[-1]["turn_retained_isolated_material_run_ids"] == []
    assert finalized[-1]["state"]["completion_certificate"]["problems"] == []
