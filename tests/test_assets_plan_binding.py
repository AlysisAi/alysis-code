from __future__ import annotations

from types import SimpleNamespace

import pytest

from alysis_code.assets import (
    AssetBriefingEntry,
    AssetError,
    TaskAssetBriefing,
    collect_referenced_asset_ids,
    infer_implicit_task_asset_briefing,
    parse_task_asset_briefing,
    serialize_task_asset_briefing,
    task_asset_briefing,
)


def test_parse_none_and_empty_asset_briefing() -> None:
    assert parse_task_asset_briefing(None) is None
    assert parse_task_asset_briefing({}) == TaskAssetBriefing(primary=[], may_need=[])


def test_asset_briefing_round_trips() -> None:
    briefing = TaskAssetBriefing(
        primary=[
            AssetBriefingEntry(
                asset_id="ast_aaaaaaaa",
                rationale="Primary rationale",
                expected_use="Use directly",
            )
        ],
        may_need=[
            AssetBriefingEntry(
                asset_id="ast_bbbbbbbb",
                rationale="May help",
                expected_use="Read if needed",
            )
        ],
    )

    assert parse_task_asset_briefing(serialize_task_asset_briefing(briefing)) == briefing


def test_asset_briefing_rejects_unknown_keys() -> None:
    with pytest.raises(AssetError, match="unsupported keys"):
        parse_task_asset_briefing({"primary": [], "extra": []})


def test_collect_referenced_asset_ids_handles_mixed_tasks() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "asset_briefing": {
                    "primary": [
                        {
                            "asset_id": "ast_aaaaaaaa",
                            "rationale": "A",
                            "expected_use": "Use A",
                        }
                    ],
                    "may_need": [
                        {
                            "asset_id": "ast_bbbbbbbb",
                            "rationale": "B",
                            "expected_use": "Use B",
                        }
                    ],
                },
            },
            {"id": "T02"},
        ]
    }

    assert collect_referenced_asset_ids(plan) == {"ast_aaaaaaaa", "ast_bbbbbbbb"}
    assert task_asset_briefing(plan["tasks"][1]) is None


def test_implicit_asset_briefing_does_not_bind_generic_file_word() -> None:
    record = SimpleNamespace(
        id="ast_marketing",
        title="Marketing screenshot",
        original_filename="hero.png",
        description="Landing page visual reference.",
        stored_path="assets/raw/ast_marketing/hero.png",
        deleted_at=None,
        pinned=False,
    )
    task = {
        "id": "T01",
        "title": "Update file parser",
        "description": "Modify the parser implementation file and tests.",
        "acceptance_criteria": ["Parser file changes are covered."],
        "estimated_files": ["src/parser.py"],
        "write_scope": ["src/parser.py"],
    }

    assert infer_implicit_task_asset_briefing(task=task, records=[record]) is None


def test_implicit_asset_briefing_binds_single_uploaded_file_reference() -> None:
    record = SimpleNamespace(
        id="ast_uploaded",
        title="Customer brief",
        original_filename="brief.md",
        description="Customer requirements.",
        stored_path="assets/raw/ast_uploaded/brief.md",
        deleted_at=None,
        pinned=False,
    )
    task = {
        "id": "T01",
        "title": "Update README using the uploaded file",
        "description": "Use the uploaded file as the source of truth.",
        "acceptance_criteria": ["README reflects the uploaded guidance."],
        "estimated_files": ["README.md"],
        "write_scope": ["README.md"],
    }

    briefing = infer_implicit_task_asset_briefing(task=task, records=[record])

    assert briefing is not None
    assert [entry.asset_id for entry in briefing.primary] == ["ast_uploaded"]
