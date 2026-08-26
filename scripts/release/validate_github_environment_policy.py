#!/usr/bin/env python3
"""Validate GitHub release-environment reviewer and tag restrictions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class EnvironmentPolicyError(ValueError):
    """Raised when a secret-bearing release environment is under-protected."""


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentPolicyError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EnvironmentPolicyError(f"{label} must be a JSON object")
    return value


def validate_environment_policy(
    environment: dict[str, Any],
    branch_policies: dict[str, Any],
    *,
    expected_name: str,
    required_pattern: str,
) -> None:
    if environment.get("name") != expected_name:
        raise EnvironmentPolicyError("environment name does not match the required release gate")
    if environment.get("can_admins_bypass") is not False:
        raise EnvironmentPolicyError("environment must disallow administrator bypass")
    rules = environment.get("protection_rules")
    if not isinstance(rules, list):
        raise EnvironmentPolicyError("environment protection_rules must be an array")
    reviewer_rules = [
        rule
        for rule in rules
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers"
    ]
    if len(reviewer_rules) != 1:
        raise EnvironmentPolicyError("environment must have exactly one required_reviewers rule")
    reviewer_rule = reviewer_rules[0]
    if reviewer_rule.get("prevent_self_review") is not True:
        raise EnvironmentPolicyError("environment must prevent self review")
    reviewers = reviewer_rule.get("reviewers")
    if not isinstance(reviewers, list) or not reviewers:
        raise EnvironmentPolicyError("environment must configure at least one required reviewer")
    reviewer_ids: set[tuple[str, int]] = set()
    for entry in reviewers:
        if not isinstance(entry, dict) or entry.get("type") not in {"User", "Team"}:
            raise EnvironmentPolicyError("environment reviewer entry is invalid")
        reviewer = entry.get("reviewer")
        reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
        if not isinstance(reviewer_id, int) or isinstance(reviewer_id, bool) or reviewer_id <= 0:
            raise EnvironmentPolicyError("environment reviewer has no positive immutable id")
        reviewer_ids.add((entry["type"], reviewer_id))
    if len(reviewer_ids) != len(reviewers):
        raise EnvironmentPolicyError("environment reviewer list contains duplicates")
    deployment = environment.get("deployment_branch_policy")
    if deployment != {"protected_branches": False, "custom_branch_policies": True}:
        raise EnvironmentPolicyError("environment must use explicit custom deployment tag policies")
    if not any(isinstance(rule, dict) and rule.get("type") == "branch_policy" for rule in rules):
        raise EnvironmentPolicyError("environment protection_rules is missing branch_policy")

    policies = branch_policies.get("branch_policies")
    total_count = branch_policies.get("total_count")
    if (
        not isinstance(policies, list)
        or not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count != len(policies)
    ):
        raise EnvironmentPolicyError("deployment branch policy response is incomplete")
    if len(policies) != 1 or not isinstance(policies[0], dict):
        raise EnvironmentPolicyError(
            f"environment deployment policy must contain only {required_pattern!r}"
        )
    policy = policies[0]
    policy_id = policy.get("id")
    if (
        policy.get("name") != required_pattern
        or not isinstance(policy_id, int)
        or isinstance(policy_id, bool)
        or policy_id <= 0
    ):
        raise EnvironmentPolicyError(
            f"environment deployment policy must contain only {required_pattern!r}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment_json", type=Path)
    parser.add_argument("branch_policies_json", type=Path)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--required-pattern", required=True)
    args = parser.parse_args(argv)
    try:
        validate_environment_policy(
            _object(args.environment_json, "environment response"),
            _object(args.branch_policies_json, "branch policy response"),
            expected_name=args.environment,
            required_pattern=args.required_pattern,
        )
    except EnvironmentPolicyError as exc:
        print(f"GitHub environment policy validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"GitHub environment policy validation passed: {args.environment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
