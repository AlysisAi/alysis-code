from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from alysis_code.config import AppConfig
from alysis_code.execution_budget import compute_execution_prompt_budget_inputs
from alysis_code.execution_context import (
    ExecutionContextBudgetError,
    build_task_context_pack_result,
)
from alysis_code.execution_shared import (
    _estimate_initial_execution_request_tokens,
    build_task_execution_instruction_bundle,
)
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.swarm_worker import run_task_worker
from alysis_code.token_budget import estimate_tokens


def _cfg(context=32_000):
    cfg = AppConfig(model="context-test", assets={"enabled": False})
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "context-test": {
                    "context_window_tokens": context,
                    "max_output_tokens": 1024,
                }
            }
        }
    }
    return cfg


def _task():
    return {
        "id": "T1",
        "title": "Retain exact requirements",
        "description": "Required description " * 150 + "描述末尾 Ω",
        "acceptance_criteria": [f"criterion-{i}: 条件" for i in range(45)],
        "write_scope": [f"src/module_{i}.py" for i in range(35)],
        "estimated_files": [f"tests/test_module_{i}.py" for i in range(35)],
        "dependencies": ["T0"],
        "branch": "feat/task",
        "status": "planned",
    }


def _plan():
    return {
        "schema_version": 2,
        "project_goal": "Supplementary overview " * 200,
        "summary": "Optional summary " * 1000,
        "requirements": [f"global requirement {i}" for i in range(90)],
        "tasks": [{"id": f"T{i}", "title": f"Other task {i}"} for i in range(100)],
        "assets": [],
    }


@pytest.mark.parametrize("budget", [0, 1, 128, 700])
def test_impossible_budget_rejects_instead_of_shortening_task(budget):
    with pytest.raises(ExecutionContextBudgetError) as caught:
        build_task_context_pack_result(
            cfg=_cfg(),
            role_model="context-test",
            plan=_plan(),
            task=_task(),
            instruction_token_budget=budget,
            leading_sections=["## Arbitrary required heading\nNever change protected.bin"],
        )
    error = caught.value
    assert error.available_tokens == budget
    assert error.required_tokens > budget
    assert error.to_payload()["error_code"] == "execution_context_budget_exceeded"
    assert "Increase the context allowance" in str(error)
    assert "protected.bin" not in json.dumps(error.to_payload())


def test_minimum_fitting_pack_keeps_all_mandatory_content_and_omits_only_optional_sections():
    task, plan = _task(), _plan()
    original = copy.deepcopy((task, plan))
    leading = "## Σύμβαση\nKeep every named scope entry.\n" + "required marker\n" * 40
    kwargs = dict(
        cfg=_cfg(), role_model="context-test", plan=plan, task=task, leading_sections=[leading]
    )
    with pytest.raises(ExecutionContextBudgetError) as caught:
        build_task_context_pack_result(**kwargs, instruction_token_budget=0)
    # Header digits are charged too; allow a few tokens for the changed budget label.
    budget = caught.value.required_tokens + 4
    result = build_task_context_pack_result(**kwargs, instruction_token_budget=budget)
    assert result.truncation_strategy == "execution_priority_mandatory_only"
    assert estimate_tokens(result.content) <= budget
    assert result.content == result.artifact_text
    assert leading.strip() in result.content
    for key in ("description", "title", "branch", "status"):
        assert task[key] in result.content
    for key in ("acceptance_criteria", "write_scope", "estimated_files", "dependencies"):
        assert all(value in result.content for value in task[key])
    assert "## Execution Rules" in result.content
    assert "## Plan Summary" not in result.content
    assert "## Selected Assets" not in result.content
    assert "## Relevant Assets" not in result.content
    assert "head_tail" not in result.truncation_strategy
    assert (task, plan) == original


@pytest.mark.parametrize("non_interactive", [False, True])
def test_bundle_admission_charges_actual_serialized_instruction(
    monkeypatch, tmp_path, non_interactive
):
    cfg = _cfg()
    task = {"id": "T1", "description": ('\\"\n' * 250) + "END"}
    args = dict(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        non_interactive=non_interactive,
        subagents_enabled=False,
    )
    inputs = compute_execution_prompt_budget_inputs(**args)
    base = dict(plan={"schema_version": 2}, task=task, role_model=cfg.model, **args)
    successful = build_task_execution_instruction_bundle(**base)
    actual = _estimate_initial_execution_request_tokens(
        root=tmp_path,
        prefix_messages=inputs.prefix_messages,
        tool_list=inputs.tool_list,
        instruction=successful.instruction,
        image_paths=(),
    )
    fixed = inputs.budget.pinned_prefix_token_estimate + inputs.budget.tool_schema_token_estimate
    assert successful.final_instruction_token_estimate == actual - fixed
    assert actual - fixed > estimate_tokens(successful.instruction)
    # A raw-text allowance fits the words but not their serialized request representation.
    raw_allowance = estimate_tokens(successful.instruction)
    narrowed = replace(
        inputs, budget=replace(inputs.budget, final_instruction_budget=raw_allowance)
    )
    monkeypatch.setattr(
        "alysis_code.execution_shared.compute_execution_prompt_budget_inputs", lambda **_: narrowed
    )
    with pytest.raises(ExecutionContextBudgetError):
        build_task_execution_instruction_bundle(**base)


def test_soft_headroom_failure_retains_hard_fitting_instructions(monkeypatch, tmp_path):
    cfg = _cfg()
    task = _task()
    leading = "## Required context\nPreserve protected.txt"
    args = dict(
        cfg=cfg,
        root=tmp_path,
        plan=_plan(),
        task=task,
        role_model=cfg.model,
        mode="auto",
        yes=True,
        leading_sections=[leading],
        subagents_enabled=False,
    )
    ordinary = build_task_execution_instruction_bundle(**args)
    monkeypatch.setattr(
        "alysis_code.execution_shared._resolve_startup_headroom_target",
        lambda **_: (32000, 100, 50),
    )
    result = build_task_execution_instruction_bundle(
        **args, managed_execution_startup_headroom=True
    )
    assert result.instruction == ordinary.instruction
    assert result.final_instruction_token_estimate <= result.budget.final_instruction_budget
    payload = result.to_budget_artifact_payload()
    assert payload["startup_headroom_adjustment_applied"] is False
    assert payload["startup_headroom_tokens"] < 0
    assert "retained complete instructions" in payload["startup_headroom_adjustment_reason"]


def test_startup_rebuild_keeps_authoritative_verification_context(monkeypatch, tmp_path):
    cfg = _cfg()
    task = {"id": "T1", "description": "Implement only the selected task."}
    args = dict(
        cfg=cfg,
        root=tmp_path,
        plan=_plan(),
        task=task,
        role_model=cfg.model,
        mode="auto",
        yes=True,
        authoritative_verification_commands=["native-check --all"],
        leading_sections=["## Required context\nKeep protected.txt"],
        subagents_enabled=False,
    )
    ordinary = build_task_execution_instruction_bundle(**args)
    fixed = (
        ordinary.budget.pinned_prefix_token_estimate + ordinary.budget.tool_schema_token_estimate
    )
    target = fixed + ordinary.final_instruction_token_estimate - 150
    monkeypatch.setattr(
        "alysis_code.execution_shared._resolve_startup_headroom_target",
        lambda **_: (32000, target + 1, target),
    )
    result = build_task_execution_instruction_bundle(
        **args, managed_execution_startup_headroom=True
    )
    payload = result.to_budget_artifact_payload()
    assert payload["startup_headroom_adjustment_applied"] is True
    assert payload["initial_request_token_estimate"] <= target
    assert "native-check --all" in result.instruction
    assert "## Required context\nKeep protected.txt" in result.instruction
    assert task["description"] in result.instruction


def test_swarm_returns_persistable_preflight_failure_without_executor(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Keep full task",
        description="Required " * 5000,
        estimated_files=["src/owned.py"],
        branch="feat/task",
    )
    save_plan(paths, plan)
    worktree = paths.run_dir / "worktrees" / task["id"] / "repo"
    worktree.mkdir(parents=True)
    # Prior/concurrent sessions and a stale copied log must not become evidence
    # for this task: no new agent session will be started.
    paths.execution_sessions_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_logs_dir.mkdir(parents=True, exist_ok=True)
    for session_id in ("foreign", task["id"]):
        (paths.execution_sessions_dir / f"{session_id}.jsonl").write_text("old session")
        (paths.execution_sessions_dir / session_id).mkdir()
    stale_log = paths.execution_logs_dir / f"{task['id']}.jsonl"
    stale_log.write_text("prior attempt")

    def unexpected(**kwargs):
        pytest.fail("No executor may receive a truncated instruction")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", unexpected)
    result = run_task_worker(
        task=task,
        plan=plan,
        worktree_repo_path=worktree,
        base_branch="main",
        run_paths=paths,
        cfg=_cfg(4096),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override=None,
        no_log=False,
        verify_mode="off",
    )
    assert result.success is False
    assert result.failure_reason == "execution_context_budget_exceeded"
    assert result.agent_exit_code == 1
    assert not result.salvaged_agent_exception
    assert result.changed_files == []
    assert result.log_path == ""
    pointer = json.loads((repo / result.log_pointer_path).read_text())
    assert pointer["log_retained"] is False
    assert pointer["session_artifacts_retained"] is False
    assert pointer["source_log_path"] is None
    assert pointer["session_id"] is None
    assert stale_log.read_text() == "prior attempt"
    assert "Increase the context allowance" in (repo / result.report_path).read_text()
    assert (repo / result.patch_path).read_text() == ""
    budget = json.loads((paths.execution_dir / "budgets" / f"{task['id']}.json").read_text())
    assert budget["required_instruction_tokens"] > budget["available_instruction_tokens"]
    assert json.loads(json.dumps(result.to_json()))["success"] is False
