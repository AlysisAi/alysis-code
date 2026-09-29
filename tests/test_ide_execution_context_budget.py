from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.config import AppConfig
from alysis_code.execution_context import ExecutionContextBudgetError
from alysis_code.forge import ensure_execution_dirs, make_run_paths
from alysis_code.ide import forge_protocol
from alysis_code.surface import NoopSurface


class _RecordingSurface(NoopSurface):
    def __init__(self) -> None:
        self.node_states: list[tuple[str, str]] = []
        self.worker_states: list[tuple[str, str]] = []

    def emit_plan_node_updated(self, task_id, status, title) -> None:
        self.node_states.append((task_id, status))

    def emit_swarm_worker_state_changed(self, task_id, state, **kwargs) -> None:
        self.worker_states.append((task_id, state))


def test_ide_execution_budget_rejection_persists_block_and_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = make_run_paths(root=tmp_path, run_id="budget-test")
    ensure_execution_dirs(paths)
    record = forge_protocol.ForgePlanRecord(session_id="session", plan_id="plan", paths=paths)
    task = {
        "id": "T1",
        "title": "Preserve required instructions",
        "description": "Complete task",
        "status": "todo",
        "dependencies": [],
        "write_scope": ["src/task.py"],
    }
    plan = {"schema_version": 2, "project_goal": "Budget safety", "tasks": [task]}
    error = ExecutionContextBudgetError(available_tokens=300, required_tokens=900)

    def reject_pack(**kwargs):
        assert kwargs["allow_write_globs"] == ["src/task.py"]
        raise error

    def forbidden(**kwargs):
        pytest.fail("Rejected instructions must not capture a workspace baseline or dispatch")

    monkeypatch.setattr(forge_protocol, "build_task_execution_instruction_bundle", reject_pack)
    monkeypatch.setattr(forge_protocol, "capture_task_local_workspace_baseline", forbidden)
    monkeypatch.setattr(
        forge_protocol,
        "resolve_managed_task_step_budget",
        lambda **kwargs: SimpleNamespace(to_payload=lambda: {"resolved_max_steps": 4}),
    )
    surface = _RecordingSurface()

    result = forge_protocol._run_single_review_task(
        record=record,
        plan=plan,
        task=task,
        cfg=AppConfig(model="offline"),
        surface=surface,
        fallback_verify_commands=[],
        max_steps=4,
        no_log=True,
        agent_runner=forbidden,
    )

    assert result["status"] == "blocked"
    assert result["success"] is False
    assert result["context_artifact_id"] is None
    assert result["patch_artifact_id"] is None
    assert result["changed_files"] == []
    assert str(error) in result["summary"]
    assert surface.node_states == [("T1", "in_progress"), ("T1", "blocked")]
    assert surface.worker_states == [("T1", "running"), ("T1", "blocked")]
    saved_plan = json.loads(paths.plan_json_path.read_text())
    assert saved_plan["tasks"][0]["status"] == "blocked"
    report = (paths.execution_reports_dir / "T1.md").read_text()
    assert "blocked before execution" in report
    assert str(error) in report
    budget = json.loads((paths.execution_budgets_dir / "T1.json").read_text())
    assert budget == {**error.to_payload(), "step_budget": {"resolved_max_steps": 4}}
    assert not (paths.execution_context_dir / "T1_context.md").exists()
    assert not (paths.execution_patches_dir / "T1.diff").exists()
