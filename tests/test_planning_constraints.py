from __future__ import annotations

from pathlib import Path

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionKind,
    build_acceptance_contract,
)
from alysis_code.planning_constraints import (
    extract_planning_scope_constraints,
    filter_scope_entries_for_planning_constraints,
    planning_constraints_from_payload,
    task_has_target_root_scope,
    task_scope_constraint_violations,
    update_plan_planning_constraints,
)


def test_extracts_target_root_and_decoy_path_constraints() -> None:
    constraints = extract_planning_scope_constraints(
        "Fix APP_REGION precedence in services/api. services/worker is a decoy."
    )

    assert [item.path for item in constraints.target_roots] == ["services/api"]
    assert [item.path for item in constraints.decoy_roots] == ["services/worker"]
    assert constraints.decoy_roots[0].reason_code == "decoy_path_constraint"


def test_ungrounded_slash_prose_does_not_create_target_root() -> None:
    workspace_context = {
        "top_level_entries": [{"path": "src", "type": "dir"}, {"path": "Cargo.toml"}],
        "observed_paths": ["src/lib.rs", "tests/parser.rs"],
        "manifests": [{"path": "Cargo.toml", "kind": "rust"}],
    }

    constraints = extract_planning_scope_constraints(
        "Implement the key/value parser and add regression tests.",
        workspace_context=workspace_context,
    )

    assert constraints.target_roots == ()


def test_decoy_language_does_not_poison_later_target_path() -> None:
    constraints = extract_planning_scope_constraints(
        "worker is a decoy, fix services/api config precedence"
    )

    assert [item.path for item in constraints.target_roots] == ["services/api"]
    assert constraints.decoy_roots == ()


def test_maps_unique_service_leaf_names_from_workspace_context() -> None:
    workspace_context = {
        "manifests": [
            {"path": "services/api/package.json", "kind": "node"},
            {"path": "services/worker/package.json", "kind": "node"},
        ],
        "observed_paths": [
            "services/api/src/config.ts",
            "services/worker/src/config.ts",
        ],
    }

    constraints = extract_planning_scope_constraints(
        "Only fix API. Worker is a decoy and should remain unchanged.",
        workspace_context=workspace_context,
    )

    assert [item.path for item in constraints.target_roots] == ["services/api"]
    assert [item.path for item in constraints.decoy_roots] == ["services/worker"]


def test_leaf_decoy_language_does_not_poison_later_target_name() -> None:
    workspace_context = {
        "manifests": [
            {"path": "services/api/package.json", "kind": "node"},
            {"path": "services/worker/package.json", "kind": "node"},
        ],
    }

    constraints = extract_planning_scope_constraints(
        "worker is a decoy, fix API config precedence",
        workspace_context=workspace_context,
    )

    assert [item.path for item in constraints.target_roots] == ["services/api"]
    assert [item.path for item in constraints.decoy_roots] == ["services/worker"]


def test_decoy_task_scope_violates_planning_constraints() -> None:
    constraints = extract_planning_scope_constraints(
        "Stay inside services/api. Do not touch services/worker."
    )
    task = {
        "title": "Fix worker config",
        "description": "Update worker behavior.",
        "acceptance_criteria": ["Worker config is updated."],
        "estimated_files": ["services/worker/src/config.ts"],
        "write_scope": ["services/worker/src/config.ts"],
    }

    violations = task_scope_constraint_violations(task, constraints)

    assert len(violations) == 1
    assert violations[0].classification == "forbidden_root"
    assert violations[0].constraint_path == "services/worker"


def test_acceptance_contract_preserves_blocked_planning_scope(tmp_path: Path) -> None:
    constraints = extract_planning_scope_constraints(
        "Only modify services/api. Do not touch services/worker."
    )

    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Only modify services/api. Do not touch services/worker.",
        planning_constraints=constraints,
    )

    preservation_paths = {
        path
        for criterion in contract.criteria
        if criterion.kind == AcceptanceCriterionKind.PRESERVATION_UNCHANGED_PATH
        for path in criterion.paths
    }
    assert "services/worker" in preservation_paths


def test_outside_target_root_requires_explicit_shared_evidence() -> None:
    constraints = extract_planning_scope_constraints("Only modify packages/web.")
    out_of_scope_task = {
        "title": "Patch API config",
        "description": "Update API behavior.",
        "acceptance_criteria": ["API behavior changes."],
        "estimated_files": ["packages/api/src/config.ts"],
        "write_scope": ["packages/api/src/config.ts"],
    }
    shared_task = {
        "title": "Patch shared package for web",
        "description": "Update shared dependency used by packages/web.",
        "acceptance_criteria": ["Shared dependency keeps web behavior correct."],
        "estimated_files": ["packages/shared/src/config.ts"],
        "write_scope": ["packages/shared/src/config.ts"],
    }

    assert task_scope_constraint_violations(out_of_scope_task, constraints)
    assert task_scope_constraint_violations(shared_task, constraints) == []


def test_filter_scope_entries_removes_blocked_and_out_of_target_paths() -> None:
    constraints = planning_constraints_from_payload(
        {
            "target_roots": [{"path": "services/api", "reason_code": "user_target_root"}],
            "decoy_roots": [{"path": "services/worker", "reason_code": "decoy_path_constraint"}],
        }
    )
    task = {
        "title": "Fix API config",
        "description": "Update services/api only.",
        "acceptance_criteria": [],
    }

    kept, violations = filter_scope_entries_for_planning_constraints(
        ["services/api/src/config.ts", "services/worker/src/config.ts", "packages/web/src/app.ts"],
        task=task,
        constraints=constraints,
    )

    assert kept == ["services/api/src/config.ts"]
    assert {item.classification for item in violations} == {"decoy_root", "outside_target_root"}


def test_constraint_violations_check_write_scope_independently() -> None:
    constraints = planning_constraints_from_payload(
        {
            "target_roots": [{"path": "services/api", "reason_code": "user_target_root"}],
            "decoy_roots": [{"path": "services/worker", "reason_code": "decoy_path_constraint"}],
        }
    )
    task = {
        "title": "Fix API config",
        "description": "Update API config.",
        "acceptance_criteria": [],
        "estimated_files": ["services/api/src/config.ts"],
        "write_scope": ["services/worker/src/config.ts"],
    }

    violations = task_scope_constraint_violations(task, constraints)

    assert len(violations) == 1
    assert violations[0].path == "services/worker/src/config.ts"
    assert violations[0].classification == "decoy_root"


def test_target_root_scope_can_come_from_write_scope_only() -> None:
    constraints = planning_constraints_from_payload(
        {"target_roots": [{"path": "services/api", "reason_code": "user_target_root"}]}
    )

    assert task_has_target_root_scope(
        {"estimated_files": [], "write_scope": ["services/api/src/config.ts"]},
        constraints,
    )


def test_explicit_retarget_replaces_old_target_roots() -> None:
    plan = {
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [{"path": "services/api", "reason_code": "user_target_root"}],
            "forbidden_roots": [],
            "decoy_roots": [],
            "unrelated_roots": [],
        }
    }

    constraints, changed = update_plan_planning_constraints(
        plan,
        text="Actually switch to fixing services/worker now.",
    )

    assert changed is True
    assert [item.path for item in constraints.target_roots] == ["services/worker"]


def test_explicit_target_unblocks_previous_decoy_without_broadening_other_targets() -> None:
    plan = {
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [{"path": "services/api", "reason_code": "user_target_root"}],
            "forbidden_roots": [],
            "decoy_roots": [{"path": "services/worker", "reason_code": "decoy_path_constraint"}],
            "unrelated_roots": [],
        }
    }

    constraints, changed = update_plan_planning_constraints(
        plan,
        text="Also fix services/worker queue shutdown.",
    )

    assert changed is True
    assert [item.path for item in constraints.target_roots] == ["services/api", "services/worker"]
    assert constraints.decoy_roots == ()
