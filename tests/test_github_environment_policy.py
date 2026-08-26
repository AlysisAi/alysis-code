from __future__ import annotations

import copy

import pytest

from scripts.release.validate_github_environment_policy import (
    EnvironmentPolicyError,
    validate_environment_policy,
)


def _environment() -> dict[str, object]:
    return {
        "name": "vscode-marketplace-beta",
        "can_admins_bypass": False,
        "protection_rules": [
            {
                "id": 1,
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {"type": "Team", "reviewer": {"id": 42, "slug": "release-maintainers"}}
                ],
            },
            {"id": 2, "type": "branch_policy"},
        ],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }


def _policies() -> dict[str, object]:
    return {"total_count": 1, "branch_policies": [{"id": 3, "name": "v*"}]}


def _validate(environment: dict[str, object], policies: dict[str, object]) -> None:
    validate_environment_policy(
        environment,
        policies,
        expected_name="vscode-marketplace-beta",
        required_pattern="v*",
    )


def test_accepts_self_review_protected_exact_tag_environment() -> None:
    _validate(_environment(), _policies())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("self_review", "prevent self review"),
        ("admin_bypass", "disallow administrator bypass"),
        ("no_reviewers", "at least one"),
        ("all_refs", "explicit custom"),
        ("broad_pattern", "only 'v\\*'"),
        ("extra_pattern", "only 'v\\*'"),
        ("missing_policy_id", "only 'v\\*'"),
        ("boolean_total", "incomplete"),
        ("duplicate_reviewer", "duplicates"),
        ("missing_branch_rule", "missing branch_policy"),
    ],
)
def test_rejects_underprotected_environment(mutation: str, message: str) -> None:
    environment = copy.deepcopy(_environment())
    policies = copy.deepcopy(_policies())
    rules = environment["protection_rules"]
    assert isinstance(rules, list) and isinstance(rules[0], dict)
    if mutation == "self_review":
        rules[0]["prevent_self_review"] = False
    elif mutation == "admin_bypass":
        environment["can_admins_bypass"] = True
    elif mutation == "no_reviewers":
        rules[0]["reviewers"] = []
    elif mutation == "all_refs":
        environment["deployment_branch_policy"] = None
    elif mutation == "broad_pattern":
        policies["branch_policies"] = [{"id": 3, "name": "*"}]
    elif mutation == "extra_pattern":
        policies["total_count"] = 2
        policies["branch_policies"] = [
            {"id": 3, "name": "v*"},
            {"id": 4, "name": "main"},
        ]
    elif mutation == "missing_policy_id":
        policies["branch_policies"] = [{"name": "v*"}]
    elif mutation == "boolean_total":
        policies["total_count"] = True
    elif mutation == "duplicate_reviewer":
        reviewers = rules[0]["reviewers"]
        assert isinstance(reviewers, list)
        reviewers.append(copy.deepcopy(reviewers[0]))
    else:
        rules.pop()

    with pytest.raises(EnvironmentPolicyError, match=message):
        _validate(environment, policies)
