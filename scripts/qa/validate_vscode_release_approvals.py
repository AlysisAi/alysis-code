#!/usr/bin/env python3
"""Bind production signoff roles to actual GitHub environment approval history."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.release.vscode_approval_policy import is_single_maintainer  # noqa: E402

ROLE_SOURCES = {
    "engineering": ("candidate", "managed-cli-signing"),
    "security_governance": ("evidence", "vscode-release-evidence"),
    "release_manager": ("promotion", "vscode-marketplace-stable"),
}
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


class ReleaseApprovalError(ValueError):
    """Raised when claimed release approvers are not backed by GitHub approvals."""


def _json(path: Path, label: str) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ReleaseApprovalError(f"{label} must be a regular JSON file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseApprovalError(f"{label} is not valid UTF-8 JSON") from exc


def _approved_users(history: object, environment: str, label: str) -> dict[str, int]:
    if not isinstance(history, list):
        raise ReleaseApprovalError(f"{label} approval history must be an array")
    approved: dict[str, int] = {}
    rejected = False
    for record in history:
        if not isinstance(record, dict):
            raise ReleaseApprovalError(f"{label} approval history contains a non-object")
        environments = record.get("environments")
        if not isinstance(environments, list):
            raise ReleaseApprovalError(f"{label} approval record has no environment list")
        names = {
            value.get("name")
            for value in environments
            if isinstance(value, dict) and isinstance(value.get("name"), str)
        }
        if environment not in names:
            continue
        state = record.get("state")
        if state == "rejected":
            rejected = True
            continue
        if state != "approved":
            continue
        user = record.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        user_id = user.get("id") if isinstance(user, dict) else None
        if (
            not isinstance(login, str)
            or LOGIN_RE.fullmatch(login) is None
            or not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
        ):
            raise ReleaseApprovalError(f"{label} approval has no immutable user identity")
        key = login.casefold()
        if key in approved and approved[key] != user_id:
            raise ReleaseApprovalError(f"{label} approval login maps to multiple user IDs")
        approved[key] = user_id
    if rejected:
        raise ReleaseApprovalError(f"{label} contains a rejected review for {environment}")
    if not approved:
        raise ReleaseApprovalError(f"{label} has no approved review for {environment}")
    return approved


def validate_approvals(
    signoff: dict[str, Any],
    histories: dict[str, object],
    *,
    run_ids: dict[str, int],
    candidate_run_attempt: int,
    evidence_run_attempt: int,
    promotion_run_attempt: int,
    channel: str = "stable",
) -> dict[str, Any]:
    if channel not in {"stable", "beta"}:
        raise ReleaseApprovalError("unsupported release channel")
    if signoff.get("channel") != f"marketplace-{channel}":
        raise ReleaseApprovalError("signoff does not approve this release channel")
    approvers = signoff.get("approvers")
    if not isinstance(approvers, dict) or set(approvers) != set(ROLE_SOURCES):
        raise ReleaseApprovalError("production signoff approver roles are not exact")
    if set(histories) != {"candidate", "evidence", "promotion"} or set(run_ids) != set(histories):
        raise ReleaseApprovalError("approval workflow sources are not exact")
    if candidate_run_attempt != 1:
        raise ReleaseApprovalError(
            "candidate approval must come from a non-rerun first-attempt workflow"
        )
    if any(
        value <= 0 for value in (*run_ids.values(), evidence_run_attempt, promotion_run_attempt)
    ):
        raise ReleaseApprovalError("approval workflow identities must be positive")
    roles: dict[str, dict[str, Any]] = {}
    immutable_ids: set[int] = set()
    immutable_logins: set[str] = set()
    for role, (source, environment) in sorted(ROLE_SOURCES.items()):
        if role == "release_manager":
            environment = f"vscode-marketplace-{channel}"
        claimed = approvers.get(role)
        if not isinstance(claimed, str) or LOGIN_RE.fullmatch(claimed) is None:
            raise ReleaseApprovalError(f"signoff approvers.{role} is not a GitHub login")
        approved = _approved_users(histories[source], environment, source)
        login_key = claimed.casefold()
        if login_key not in approved:
            raise ReleaseApprovalError(f"signoff approvers.{role} did not approve {environment}")
        user_id = approved[login_key]
        if (user_id in immutable_ids or login_key in immutable_logins) and not is_single_maintainer(
            claimed, user_id
        ):
            raise ReleaseApprovalError(
                "release approval roles must use distinct actual users unless approved "
                "by the source-pinned single maintainer"
            )
        immutable_ids.add(user_id)
        immutable_logins.add(login_key)
        roles[role] = {
            "environment": environment,
            "login": claimed,
            "source": source,
            "user_id": user_id,
            "workflow_run_id": run_ids[source],
        }
    return {
        "schema_name": "vscode-release-approval-binding",
        "schema_version": 1,
        "candidate_run_attempt": candidate_run_attempt,
        "evidence_run_attempt": evidence_run_attempt,
        "promotion_run_attempt": promotion_run_attempt,
        "roles": roles,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("signoff", type=Path)
    parser.add_argument("candidate_approvals", type=Path)
    parser.add_argument("evidence_approvals", type=Path)
    parser.add_argument("promotion_approvals", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--candidate-run-id", required=True, type=int)
    parser.add_argument("--candidate-run-attempt", required=True, type=int)
    parser.add_argument("--evidence-run-id", required=True, type=int)
    parser.add_argument("--evidence-run-attempt", required=True, type=int)
    parser.add_argument("--promotion-run-id", required=True, type=int)
    parser.add_argument("--promotion-run-attempt", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        signoff = _json(args.signoff, "production signoff")
        if not isinstance(signoff, dict):
            raise ReleaseApprovalError("production signoff must be a JSON object")
        result = validate_approvals(
            signoff,
            {
                "candidate": _json(args.candidate_approvals, "candidate"),
                "evidence": _json(args.evidence_approvals, "evidence"),
                "promotion": _json(args.promotion_approvals, "promotion"),
            },
            run_ids={
                "candidate": args.candidate_run_id,
                "evidence": args.evidence_run_id,
                "promotion": args.promotion_run_id,
            },
            candidate_run_attempt=args.candidate_run_attempt,
            evidence_run_attempt=args.evidence_run_attempt,
            promotion_run_attempt=args.promotion_run_attempt,
            channel=args.channel,
        )
    except ReleaseApprovalError as exc:
        parser.error(str(exc))
    result["production_signoff_sha256"] = hashlib.sha256(args.signoff.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
