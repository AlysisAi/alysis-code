from __future__ import annotations

from pathlib import Path
from typing import Any

from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import AssetSurface
from alysis_code.assets.usage_logger import AssetUsageLogger
from alysis_code.config import AppConfig
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.integration_gate import IntegrationGateResult
from alysis_code.plan_assistant import PlannerTurnResult, apply_plan_update
from alysis_code.replanning import (
    ReplanningTrigger,
    run_replanning_attempt,
)
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult


def _integration_result(paths) -> IntegrationGateResult:
    artifact_dir = paths.execution_integration_dir / "batch_001"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    verify_artifact = artifact_dir / "verify.txt"
    verify_artifact.write_text("verify\n", encoding="utf-8")
    result_path = artifact_dir / "result.json"
    result_path.write_text("{}\n", encoding="utf-8")
    commands_path = artifact_dir / "commands.json"
    commands_path.write_text("{}\n", encoding="utf-8")
    stdout_path = artifact_dir / "stdout.txt"
    stdout_path.write_text("stdout\n", encoding="utf-8")
    stderr_path = artifact_dir / "stderr.txt"
    stderr_path.write_text("stderr\n", encoding="utf-8")
    summary_path = artifact_dir / "summary.md"
    summary_path.write_text("failed\n", encoding="utf-8")
    return IntegrationGateResult(
        batch_index=1,
        batch_label="batch_001",
        mode="warn",
        command_source="test",
        commands=("pytest -q",),
        merged_task_ids=("T01",),
        merged_paths=("src/app.py",),
        verify_result=VerifyRunResult(
            commands=["pytest -q"],
            command_results=[
                VerifyCommandResult(
                    command="pytest -q",
                    exit_code=1,
                    output="failed",
                    stdout="failed",
                    stderr="",
                )
            ],
            artifact_path=verify_artifact,
        ),
        artifact_dir=artifact_dir,
        result_path=result_path,
        commands_path=commands_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        summary_path=summary_path,
        verify_artifact_path=verify_artifact,
    )


def test_replanner_receives_asset_bundle_and_prior_usage(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(
        plan,
        title="Fix app",
        description="Use the spec asset",
        estimated_files=["src/app.py"],
        write_scope=["src/app.py"],
    )
    save_plan(paths, plan)
    cfg = AppConfig(model="planner-model")
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    source = write_text_asset_source(repo, "spec.txt", "asset spec\n")
    record = surface.add_asset(source, title="Spec", comprehend="sync").record
    usage = AssetUsageLogger(run_paths=paths, task_id="T01")
    usage.allocation_decision(asset_id=record.id, mode="full_inline")
    usage.asset_read(asset_id=record.id, focus=False, chars=10, cached=False)
    usage.summary(primary_count=1, may_need_count=0, pinned_count=0)
    captured: dict[str, Any] = {}

    def fake_planner_runner(**kwargs: Any) -> PlannerTurnResult:
        captured.update(kwargs)
        return PlannerTurnResult(
            assistant_message="No update",
            questions=[],
            plan_update=None,
            error=None,
        )

    result = run_replanning_attempt(
        paths=paths,
        plan=load_plan(paths),
        cfg=cfg,
        api_key_override="test-key",
        requested_mode="suggest",
        batch_index=1,
        merged_task_ids=["T01"],
        integration_result=_integration_result(paths),
        trigger=ReplanningTrigger(open_integration_issues=(), trigger_reason="failed"),
        planner_runner=fake_planner_runner,
    )

    assert result.proposal_generated is False
    assert captured["run_paths"] == paths
    assert captured["prebuilt_assets_bundle"] is not None
    assert "## Available Assets" in captured["prebuilt_assets_bundle"].context.text_block
    assert record.id in captured["prebuilt_assets_bundle"].context.text_block
    assert "## Prior Attempt Asset Interaction" in captured["relevant_knowledge_section"]
    assert "## Replanner Asset Instructions" in captured["relevant_knowledge_section"]


def test_asset_briefing_update_preserve_replace_and_clear() -> None:
    plan = {
        "schema_version": 2,
        "requirements": [],
        "tasks": [
            {
                "id": "T01",
                "title": "Task",
                "description": "Do work",
                "acceptance_criteria": ["done"],
                "dependencies": [],
                "estimated_files": ["src/app.py"],
                "write_scope": ["src/app.py"],
                "status": "planned",
                "asset_briefing": {
                    "primary": [
                        {
                            "asset_id": "ast_old",
                            "rationale": "Old rationale",
                            "expected_use": "Old use",
                        }
                    ],
                    "may_need": [],
                },
            }
        ],
    }

    apply_plan_update(plan, {"tasks_update": [{"id": "T01", "title": "Task renamed"}]})
    assert plan["tasks"][0]["asset_briefing"]["primary"][0]["asset_id"] == "ast_old"

    replacement = {
        "primary": [
            {
                "asset_id": "ast_new",
                "rationale": "New rationale",
                "expected_use": "New use",
            }
        ],
        "may_need": [],
    }
    apply_plan_update(
        plan,
        {"tasks_update": [{"id": "T01", "asset_briefing": replacement}]},
    )
    assert plan["tasks"][0]["asset_briefing"] == replacement

    apply_plan_update(plan, {"tasks_update": [{"id": "T01", "asset_briefing": None}]})
    assert "asset_briefing" not in plan["tasks"][0]
