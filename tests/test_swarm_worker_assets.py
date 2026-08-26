from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import AssetSurface
from alysis_code.assets.budget_allocator import AssetInclusionDecision, TaskAssetAllocation
from alysis_code.config import AppConfig
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.mcp.manager import McpHostToolBinding
from alysis_code.swarm_worker import run_task_worker


def test_run_task_worker_mirrors_allocates_and_passes_asset_tools(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    cfg = AppConfig(model="fake-model")
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {
            "fake-model": {
                "context_window_tokens": 8192,
                "max_output_tokens": 1024,
                "supports_vision": False,
            }
        }
    }
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    asset = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "asset guidance"),
        title="Spec",
        comprehend="sync",
    ).record
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Implement scoped file",
        estimated_files=["src/in_scope.py"],
        write_scope=["src/in_scope.py"],
        branch="feat/t01",
    )
    task["asset_briefing"] = {
        "primary": [
            {"asset_id": asset.id, "rationale": "r", "expected_use": "u"},
        ],
        "may_need": [],
    }
    save_plan(paths, plan)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    captured: dict[str, object] = {}

    def fake_allocate(**kwargs):  # type: ignore[no-untyped-def]
        mirror = kwargs["mirror"]
        return TaskAssetAllocation(
            task_id=str(task["id"]),
            decisions=[
                AssetInclusionDecision(
                    asset_id=mirror.primary[0].asset_id,
                    mode="full_inline",
                    focus=None,
                    reason="small",
                )
            ],
            elapsed_ms=1,
            model=None,
            tokens_used={},
            fallback_used=False,
            fallback_reason=None,
        )

    def fake_run_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured["instruction"] = kwargs["instruction"]
        captured["tool_aliases"] = [
            binding.tool_alias for binding in kwargs["mcp_manager"].tool_bindings
        ]
        (kwargs["root"] / "src").mkdir(exist_ok=True)
        (kwargs["root"] / "src" / "in_scope.py").write_text("ok\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.allocate_task_assets", fake_allocate)
    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.create_mcp_manager",
        lambda **_kwargs: _base_mcp_manager(),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_args, **_kwargs: SimpleNamespace(
            changed_files=("src/in_scope.py",),
            patch_text="diff --git a/src/in_scope.py b/src/in_scope.py\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=plan,
        worktree_repo_path=worktree,
        base_branch="main",
        run_paths=paths,
        cfg=cfg,
        mode="auto",
        yes=True,
        max_steps=1,
        api_key_override="k",
        no_log=True,
    )

    assert result.task_id == task["id"]
    assert "## Relevant Assets" in captured["instruction"]
    assert captured["tool_aliases"] == ["echo", "asset_read", "asset_load"]
    assert (worktree / ".alysis" / "task_assets" / "manifest.json").exists()
    allocation_payload = json.loads(
        (paths.execution_asset_briefings_dir / f"{task['id']}.json").read_text("utf-8")
    )
    assert allocation_payload["attempts"][0]["decisions"][0]["asset_id"] == asset.id
    usage_events = [
        json.loads(line)
        for line in (paths.execution_asset_usage_dir / f"{task['id']}.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    assert usage_events[-1]["event"] == "summary"


def _base_mcp_manager():
    binding = McpHostToolBinding(
        tool_name="echo",
        tool_alias="echo",
        description="Echo",
        parameters={"type": "object", "properties": {}, "required": []},
        run_handler=lambda _args: {"ok": True},
    )

    class _BaseManager:
        resolved_config = type("_Resolved", (), {"has_any_config": True})()
        closed = False

        @property
        def tool_bindings(self):  # type: ignore[no-untyped-def]
            return (binding,)

        def startup_metadata(self):  # type: ignore[no-untyped-def]
            return {}

        def catalog_snapshot_metadata(self):  # type: ignore[no-untyped-def]
            return {
                "exposed_tool_aliases": ["echo"],
                "exposed_tool_names": ["echo"],
                "exposed_tool_count": 1,
            }

        def execution_context_summary(self):  # type: ignore[no-untyped-def]
            return {}

        def close(self) -> None:
            self.closed = True

    return _BaseManager()


def test_run_task_worker_persists_asset_artifacts_when_agent_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    cfg = AppConfig(model="fake-model")
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    asset = surface.add_asset(
        write_text_asset_source(tmp_path, "asset.txt", "asset guidance"),
        title="Spec",
        comprehend="sync",
    ).record
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Implement scoped file",
        estimated_files=["src/in_scope.py"],
        write_scope=["src/in_scope.py"],
        branch="feat/t01",
    )
    task["asset_briefing"] = {
        "primary": [
            {"asset_id": asset.id, "rationale": "r", "expected_use": "u"},
        ],
        "may_need": [],
    }
    save_plan(paths, plan)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    monkeypatch.setattr(
        "alysis_code.swarm_worker.allocate_task_assets",
        lambda **kwargs: TaskAssetAllocation(
            task_id=str(task["id"]),
            decisions=[
                AssetInclusionDecision(
                    asset_id=kwargs["mirror"].primary[0].asset_id,
                    mode="reference_only",
                    focus=None,
                    reason="summary enough",
                )
            ],
            elapsed_ms=1,
            model=None,
            tokens_used={},
            fallback_used=False,
            fallback_reason=None,
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_agent",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("worker failed")),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_args, **_kwargs: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [],
    )

    result = run_task_worker(
        task=task,
        plan=plan,
        worktree_repo_path=worktree,
        base_branch="main",
        run_paths=paths,
        cfg=cfg,
        mode="auto",
        yes=True,
        max_steps=1,
        api_key_override="k",
        no_log=True,
    )

    assert result.success is False
    assert (paths.execution_asset_briefings_dir / f"{task['id']}.json").exists()
    assert (paths.execution_asset_usage_dir / f"{task['id']}.jsonl").exists()
