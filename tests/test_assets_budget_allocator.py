from __future__ import annotations

import json
from pathlib import Path

from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import AssetSurface
from alysis_code.assets.budget_allocator import (
    AssetInclusionDecision,
    TaskAssetAllocation,
    allocate_task_assets,
    write_task_asset_allocation,
)
from alysis_code.assets.worker_mirror import mirror_task_assets
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall
from alysis_code.model_registry import ModelRegistry


class _AllocatorClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.responses.pop(0)


class _JsonFallbackClient:
    def __init__(self, decisions: list[dict[str, object]]) -> None:
        self.decisions = decisions
        self.calls: list[dict[str, object]] = []

    def chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(dict(kwargs))
        if kwargs.get("tools"):
            raise LLMError("tool_choice is unsupported by this provider")
        return LLMResponse(
            content=json.dumps({"decisions": self.decisions}),
            tool_calls=[],
            raw={},
        )


def _response(decisions: list[dict[str, object]]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call_1",
                name="record_task_asset_allocation",
                arguments={"decisions": decisions},
            )
        ],
        raw={},
    )


def _mirror(tmp_path: Path, count: int = 2):
    paths = create_plan_run(tmp_path, create_if_missing=True)
    cfg = AppConfig(model="fake-model")
    surface = AssetSurface(
        cfg=cfg,
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )
    records = [
        surface.add_asset(
            write_text_asset_source(tmp_path, f"a{idx}.txt", f"asset {idx}\n"),
            title=f"Asset {idx}",
            comprehend="sync",
        ).record
        for idx in range(count)
    ]
    task = {
        "id": "T01",
        "asset_briefing": {
            "primary": [
                {"asset_id": record.id, "rationale": "r", "expected_use": "u"} for record in records
            ],
            "may_need": [],
        },
    }
    workspace = tmp_path / "work"
    workspace.mkdir()
    return (
        cfg,
        paths,
        task,
        mirror_task_assets(task=task, plan={}, surface=surface, workspace_path=workspace),
    )


def test_allocator_accepts_valid_structured_decisions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg, _paths, task, mirror = _mirror(tmp_path, count=2)
    ids = [entry.asset_id for entry in mirror.primary]
    client = _AllocatorClient(
        [
            _response(
                [
                    {"asset_id": ids[0], "mode": "full_inline", "focus": None, "reason": "small"},
                    {
                        "asset_id": ids[1],
                        "mode": "reference_only",
                        "focus": None,
                        "reason": "summary enough",
                    },
                ]
            )
        ]
    )
    monkeypatch.setattr(
        "alysis_code.assets.budget_allocator.make_llm_client",
        lambda **_kwargs: client,
    )

    allocation = allocate_task_assets(
        task=task,
        plan={},
        mirror=mirror,
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        instruction_token_budget=1000,
        api_key="k",
    )

    assert allocation.fallback_used is False
    assert [decision.asset_id for decision in allocation.decisions] == ids
    assert client.calls == 1


def test_allocator_retries_then_falls_back_on_invalid_decisions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg, _paths, task, mirror = _mirror(tmp_path, count=2)
    first_id = mirror.primary[0].asset_id
    client = _AllocatorClient(
        [
            _response(
                [
                    {
                        "asset_id": first_id,
                        "mode": "reference_only",
                        "focus": None,
                        "reason": "missing other id",
                    }
                ]
            ),
            _response(
                [
                    {
                        "asset_id": "ast_missing",
                        "mode": "invalid",
                        "focus": None,
                        "reason": "bad",
                    }
                ]
            ),
        ]
    )
    monkeypatch.setattr(
        "alysis_code.assets.budget_allocator.make_llm_client",
        lambda **_kwargs: client,
    )

    allocation = allocate_task_assets(
        task=task,
        plan={},
        mirror=mirror,
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        instruction_token_budget=10,
        api_key="k",
    )

    assert allocation.fallback_used is True
    assert client.calls == 2
    assert {decision.asset_id for decision in allocation.decisions} == {
        entry.asset_id for entry in mirror.primary
    }


def test_allocator_uses_json_mode_when_tool_calls_are_unsupported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg, _paths, task, mirror = _mirror(tmp_path, count=1)
    asset_id = mirror.primary[0].asset_id
    client = _JsonFallbackClient(
        [
            {
                "asset_id": asset_id,
                "mode": "reference_only",
                "focus": None,
                "reason": "summary is enough",
            }
        ]
    )
    monkeypatch.setattr(
        "alysis_code.assets.budget_allocator.make_llm_client",
        lambda **_kwargs: client,
    )

    allocation = allocate_task_assets(
        task=task,
        plan={},
        mirror=mirror,
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        instruction_token_budget=1000,
        api_key="k",
    )

    assert allocation.fallback_used is False
    assert allocation.decisions[0].mode == "reference_only"
    assert len(client.calls) == 2
    assert client.calls[1]["response_format"] == {"type": "json_object"}


def test_allocation_artifact_appends_attempts(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    allocation = TaskAssetAllocation(
        task_id="T01",
        decisions=[
            AssetInclusionDecision(
                asset_id="ast_12345678",
                mode="reference_only",
                focus=None,
                reason="summary enough",
            )
        ],
        elapsed_ms=1,
        model=None,
        tokens_used={},
        fallback_used=True,
        fallback_reason="test",
    )

    write_task_asset_allocation(run_paths=paths, allocation=allocation, started_at="a")
    write_task_asset_allocation(run_paths=paths, allocation=allocation, started_at="b")

    payload = json.loads((paths.execution_asset_briefings_dir / "T01.json").read_text("utf-8"))
    assert [attempt["started_at"] for attempt in payload["attempts"]] == ["a", "b"]
