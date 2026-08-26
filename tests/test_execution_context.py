from __future__ import annotations

import warnings

import pytest

from alysis_code.config import AppConfig
from alysis_code.execution_context import (
    build_task_context_pack,
    build_task_context_pack_result,
    compact_plan_for_execution,
    select_relevant_assets,
    select_relevant_image_paths,
)
from alysis_code.model_registry import ModelRegistry
from alysis_code.token_budget import compute_input_budget, estimate_tokens


def _large_plan() -> dict:
    return {
        "project_goal": "Ship robust execution packs",
        "summary": "Large plan for budget tests",
        "requirements": [f"requirement {i:03d}" for i in range(120)],
        "tasks": [
            {
                "id": f"T{i:02d}",
                "title": f"Task {i}",
                "dependencies": [f"T{i - 1:02d}"] if i > 1 else [],
            }
            for i in range(1, 80)
        ],
        "assets": [
            {
                "stored_path": f".alysis/runs/r/plan/assets/spec_{i:03d}.md",
                "text_copy_path": f".alysis/runs/r/plan/assets_text/spec_{i:03d}.txt",
            }
            for i in range(90)
        ],
    }


def test_compact_plan_for_execution_truncates_tail_sections() -> None:
    compact = compact_plan_for_execution(_large_plan())
    assert len(compact["requirements_tail"]) == 20
    assert compact["requirements_tail"][0] == "requirement 100"
    assert compact["requirements_tail"][-1] == "requirement 119"
    assert len(compact["assets"]) == 25
    assert len(compact["tasks"]) == 79


def test_select_relevant_assets_prefers_filename_mentions() -> None:
    plan = _large_plan()
    task = {
        "id": "T55",
        "title": "Integrate spec_042 into parser",
        "description": "Need spec_042 details and acceptance text",
        "acceptance_criteria": [],
        "estimated_files": ["src/parser.py"],
        "write_scope": ["src/parser.py"],
    }
    with pytest.warns(DeprecationWarning):
        selected = select_relevant_assets(plan, task)
    assert selected
    assert "spec_042" in str(selected[0].get("stored_path") or "")


def test_select_relevant_image_paths_emits_deprecation_warning(tmp_path) -> None:
    plan = {"assets": []}
    task = {"id": "T01", "title": "Task"}

    with pytest.warns(DeprecationWarning):
        assert select_relevant_image_paths(plan=plan, task=task, root=tmp_path, max_images=1) == []


def test_v2_plan_bypasses_legacy_asset_selectors_without_warnings(tmp_path) -> None:
    plan = _large_plan()
    plan["schema_version"] = 2
    task = {"id": "T01", "title": "Use spec_042"}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        selected = select_relevant_assets(plan, task)
        images = select_relevant_image_paths(
            plan=plan,
            task=task,
            root=tmp_path,
            max_images=1,
        )

    assert selected == []
    assert images == []
    assert not [item for item in caught if issubclass(item.category, DeprecationWarning)]


def test_v1_plan_with_asset_briefing_bypasses_legacy_asset_selectors() -> None:
    plan = _large_plan()
    plan["schema_version"] = 1
    plan["tasks"][0]["asset_briefing"] = {
        "primary": [
            {
                "asset_id": "ast_new",
                "rationale": "Use the new asset system",
                "expected_use": "Read through asset tools",
            }
        ],
        "may_need": [],
    }
    task = plan["tasks"][0]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        selected = select_relevant_assets(plan, task)

    assert selected == []
    assert not [item for item in caught if issubclass(item.category, DeprecationWarning)]


def test_build_task_context_pack_huge_plan_stays_within_budget() -> None:
    plan = _large_plan()
    task = {
        "id": "T79",
        "title": "Implement huge context pack support",
        "description": "x " * 8000,
        "acceptance_criteria": [f"criterion {i}" for i in range(40)],
        "dependencies": ["T40", "T55"],
        "estimated_files": ["src/execution_context.py", "src/execution_shared.py"],
        "write_scope": ["src/execution_context.py", "src/execution_shared.py"],
        "branch": "feat/context-pack",
        "status": "planned",
    }
    cfg = AppConfig(model="tiny-budget")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "tiny-budget": {
                    "context_window_tokens": 4096,
                    "max_output_tokens": 3072,
                    "supports_vision": False,
                }
            }
        }
    }
    pack = build_task_context_pack(
        cfg=cfg,
        plan=plan,
        task=task,
        role_model="tiny-budget",
    )
    budget = compute_input_budget(ModelRegistry(cfg=cfg).get("tiny-budget"))
    assert estimate_tokens(pack) <= budget
    assert "TRUNCATED" in pack


def test_build_task_context_pack_respects_explicit_instruction_budget() -> None:
    plan = _large_plan()
    task = {
        "id": "T80",
        "title": "Respect explicit budget",
        "description": "y " * 6000,
        "acceptance_criteria": [f"criterion {i}" for i in range(20)],
        "dependencies": ["T10"],
        "estimated_files": ["src/execution_context.py"],
        "write_scope": ["src/execution_context.py"],
        "branch": "feat/explicit-budget",
        "status": "planned",
    }
    cfg = AppConfig(model="wide-budget")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "wide-budget": {
                    "context_window_tokens": 32000,
                    "max_output_tokens": 2048,
                    "supports_vision": False,
                }
            }
        }
    }

    implicit_budget = compute_input_budget(ModelRegistry(cfg=cfg).get("wide-budget"))
    result = build_task_context_pack_result(
        cfg=cfg,
        plan=plan,
        task=task,
        role_model="wide-budget",
        instruction_token_budget=700,
    )

    assert implicit_budget > result.instruction_token_budget
    assert result.instruction_token_budget == 700
    assert result.instruction_token_estimate <= 700
    assert result.truncated is True
    assert result.truncation_strategy.startswith("execution_priority")
    assert "TRUNCATED" in result.content


def test_build_task_context_pack_prioritizes_assets_task_and_rules_when_budget_is_tight() -> None:
    plan = _large_plan()
    task = {
        "id": "T81",
        "title": "Implement spec_042 parser retry handling",
        "description": "Need the attached spec and execution rules to survive startup trimming. "
        * 80,
        "acceptance_criteria": [f"criterion {i}" for i in range(20)],
        "dependencies": ["T10"],
        "estimated_files": ["src/execution_context.py", "src/parser.py"],
        "write_scope": ["src/execution_context.py", "src/parser.py"],
        "branch": "feat/priority-pack",
        "status": "planned",
    }
    cfg = AppConfig(model="wide-budget")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "wide-budget": {
                    "context_window_tokens": 32000,
                    "max_output_tokens": 2048,
                    "supports_vision": False,
                }
            }
        }
    }

    result = build_task_context_pack_result(
        cfg=cfg,
        plan=plan,
        task=task,
        role_model="wide-budget",
        instruction_token_budget=900,
    )

    assert result.instruction_token_estimate <= 900
    assert "## Selected Assets" in result.content
    assert ".alysis/runs/r/plan/assets/spec_042.md" in result.content
    assert ".alysis/runs/r/plan/assets_text/spec_042.txt" in result.content
    assert "## Task Specification" in result.content
    assert "## Execution Rules" in result.content
    assert "You may read attached plan assets as needed" in result.content
    assert "earlier requirements omitted" in result.content
    assert "additional tasks omitted" in result.content


def test_v1_empty_legacy_assets_section_still_renders() -> None:
    plan = _large_plan()
    plan["schema_version"] = 1
    plan["assets"] = []
    task = {
        "id": "T01",
        "title": "Task",
        "description": "Do work",
        "acceptance_criteria": ["done"],
        "dependencies": [],
        "estimated_files": ["src/app.py"],
        "write_scope": ["src/app.py"],
        "status": "planned",
    }

    result = build_task_context_pack_result(
        cfg=AppConfig(model="wide-budget"),
        plan=plan,
        task=task,
        role_model="wide-budget",
        instruction_token_budget=4000,
    )

    assert "## Selected Assets" in result.content
    assert "- (none)" in result.content


def test_v2_empty_legacy_assets_section_is_suppressed() -> None:
    plan = _large_plan()
    plan["schema_version"] = 2
    plan["assets"] = []
    task = {
        "id": "T01",
        "title": "Task",
        "description": "Do work",
        "acceptance_criteria": ["done"],
        "dependencies": [],
        "estimated_files": ["src/app.py"],
        "write_scope": ["src/app.py"],
        "status": "planned",
    }

    result = build_task_context_pack_result(
        cfg=AppConfig(model="wide-budget"),
        plan=plan,
        task=task,
        role_model="wide-budget",
        instruction_token_budget=4000,
    )

    assert "## Selected Assets" not in result.content


def test_v1_asset_briefing_suppresses_legacy_assets_section() -> None:
    plan = _large_plan()
    plan["schema_version"] = 1
    task = {
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
                    "asset_id": "ast_new",
                    "rationale": "Use the new asset system",
                    "expected_use": "Read through asset tools",
                }
            ],
            "may_need": [],
        },
    }
    plan["tasks"] = [task]

    result = build_task_context_pack_result(
        cfg=AppConfig(model="wide-budget"),
        plan=plan,
        task=task,
        role_model="wide-budget",
        instruction_token_budget=4000,
    )

    assert "## Selected Assets" not in result.content
