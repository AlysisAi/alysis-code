from __future__ import annotations

import copy

import pytest

from scripts.qa.validate_vscode_release_approvals import (
    ReleaseApprovalError,
    validate_approvals,
)


def _approval(environment: str, login: str, user_id: int, *, state: str = "approved") -> dict:
    return {
        "state": state,
        "comment": "reviewed exact candidate",
        "environments": [{"id": user_id + 1000, "name": environment}],
        "user": {"login": login, "id": user_id, "type": "User"},
    }


def _inputs() -> tuple[dict, dict[str, object]]:
    signoff = {
        "channel": "marketplace-stable",
        "approvers": {
            "engineering": "eng-reviewer",
            "security_governance": "security-reviewer",
            "release_manager": "release-reviewer",
        },
    }
    histories = {
        "candidate": [_approval("managed-cli-signing", "eng-reviewer", 101)],
        "evidence": [_approval("vscode-release-evidence", "security-reviewer", 202)],
        "promotion": [_approval("vscode-marketplace-stable", "release-reviewer", 303)],
    }
    return signoff, histories


def _validate(signoff: dict, histories: dict[str, object], *, channel: str = "stable") -> dict:
    return validate_approvals(
        signoff,
        histories,
        run_ids={"candidate": 11, "evidence": 22, "promotion": 33},
        candidate_run_attempt=1,
        evidence_run_attempt=2,
        promotion_run_attempt=3,
        channel=channel,
    )


def test_release_approvals_bind_three_roles_to_actual_distinct_reviews() -> None:
    signoff, histories = _inputs()
    receipt = _validate(signoff, histories)

    assert receipt["roles"]["engineering"]["user_id"] == 101
    assert receipt["roles"]["security_governance"]["workflow_run_id"] == 22
    assert receipt["roles"]["release_manager"]["environment"] == "vscode-marketplace-stable"


def test_beta_requires_approval_in_its_own_environment() -> None:
    signoff, histories = _inputs()
    signoff["channel"] = "marketplace-beta"
    with pytest.raises(ReleaseApprovalError, match="vscode-marketplace-beta"):
        _validate(signoff, histories, channel="beta")
    histories["promotion"] = [_approval("vscode-marketplace-beta", "release-reviewer", 303)]
    assert (
        _validate(signoff, histories, channel="beta")["roles"]["release_manager"]["environment"]
        == "vscode-marketplace-beta"
    )
    with pytest.raises(ReleaseApprovalError, match="release channel"):
        _validate(signoff, histories)


@pytest.mark.parametrize("mutation", ["fabricated", "wrong_environment", "rejected", "same_user"])
def test_release_approvals_reject_unbacked_or_non_independent_claims(mutation: str) -> None:
    signoff, histories = _inputs()
    if mutation == "fabricated":
        signoff["approvers"]["engineering"] = "someone-else"
    elif mutation == "wrong_environment":
        histories["candidate"][0]["environments"][0]["name"] = "managed-cli-native-signing"
    elif mutation == "rejected":
        histories["evidence"].append(
            _approval("vscode-release-evidence", "other-reviewer", 909, state="rejected")
        )
    else:
        signoff["approvers"]["release_manager"] = "eng-reviewer"
        histories["promotion"] = [_approval("vscode-marketplace-stable", "eng-reviewer", 101)]

    with pytest.raises(ReleaseApprovalError):
        _validate(signoff, histories)


def test_release_approvals_reject_login_rebound_to_multiple_ids() -> None:
    signoff, histories = _inputs()
    altered = copy.deepcopy(histories)
    altered["candidate"].append(_approval("managed-cli-signing", "eng-reviewer", 999))

    with pytest.raises(ReleaseApprovalError, match="multiple user IDs"):
        _validate(signoff, altered)


@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_single_maintainer_can_fill_all_roles_with_real_approvals(channel: str) -> None:
    signoff, _ = _inputs()
    signoff["channel"] = f"marketplace-{channel}"
    signoff["approvers"] = dict.fromkeys(signoff["approvers"], "Perdikis10")
    histories = {
        "candidate": [_approval("managed-cli-signing", "Perdikis10", 190930654)],
        "evidence": [_approval("vscode-release-evidence", "Perdikis10", 190930654)],
        "promotion": [_approval(f"vscode-marketplace-{channel}", "Perdikis10", 190930654)],
    }
    receipt = _validate(signoff, histories, channel=channel)
    assert {role["user_id"] for role in receipt["roles"].values()} == {190930654}
    histories["evidence"] = []
    with pytest.raises(ReleaseApprovalError, match="no approved review"):
        _validate(signoff, histories, channel=channel)


def test_single_maintainer_exception_requires_immutable_account_identity() -> None:
    signoff, _ = _inputs()
    signoff["approvers"] = dict.fromkeys(signoff["approvers"], "Perdikis10")
    histories = {
        "candidate": [_approval("managed-cli-signing", "Perdikis10", 999)],
        "evidence": [_approval("vscode-release-evidence", "Perdikis10", 999)],
        "promotion": [_approval("vscode-marketplace-stable", "Perdikis10", 999)],
    }
    with pytest.raises(ReleaseApprovalError, match="distinct actual users"):
        _validate(signoff, histories)
