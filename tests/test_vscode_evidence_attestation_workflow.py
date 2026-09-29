from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "vscode-extension-evidence.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_evidence_workflow_is_protected_tag_bound_and_candidate_bound() -> None:
    workflow = _workflow()

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "environment: vscode-release-evidence" in workflow
    assert "needs: validate-environment-policy" in workflow
    assert "validate_github_environment_policy.py" in workflow
    assert "vscode-installed-live-provider-qa" in workflow
    assert "vscode-real-wsl-acceptance" in workflow
    assert "vscode-real-remote-ssh-acceptance" in workflow
    assert "--required-pattern 'v*'" in workflow
    assert 'GH_API_VERSION: "2026-03-10"' in workflow
    assert 'test "$EVIDENCE_CONFIRMATION" = "attest-release-evidence"' in workflow
    assert '[[ "$EVIDENCE_INVENTORY_SHA256" =~ ^[0-9a-f]{64}$ ]]' in workflow
    assert 'test "$GITHUB_REF" = "refs/tags/$RELEASE_TAG"' in workflow
    assert ".github/workflows/managed-cli-vsix-release.yml" in workflow
    assert ".github/workflows/vscode-installed-live-provider-qa.yml" in workflow
    assert "installed_live_provider_run_id:" in workflow
    assert ".github/workflows/vscode-extension-untrusted-workspace.yml" in workflow
    assert ".github/workflows/vscode-remote-acceptance.yml" in workflow
    assert "untrusted_workspace_run_id:" in workflow
    assert "remote_acceptance_run_id:" in workflow
    assert "workflow_run_attempts:" in workflow
    assert "WORKFLOW_RUN_ATTEMPTS_JSON: ${{ inputs.workflow_run_attempts }}" in workflow
    assert "fromJSON(inputs.workflow_run_attempts).installed_live_provider" in workflow
    assert "fromJSON(inputs.workflow_run_attempts).untrusted_workspace" in workflow
    assert '.run_attempt | type == "number"' in workflow
    assert workflow.count(".run_attempt == $run_attempt") == 2
    assert ". >= $wsl_attempt" in workflow
    assert ". >= $remote_ssh_attempt" in workflow
    assert '.conclusion == "success"' in workflow
    assert ".head_sha == $source_sha" in workflow
    assert ".head_branch == $release_tag" in workflow


def test_evidence_workflow_validates_attests_and_uploads_exact_bundle() -> None:
    workflow = _workflow()

    assert "scripts/qa/validate_vscode_promotion_bundle.py" in workflow
    assert '--candidate-run-id "$CANDIDATE_RUN_ID"' in workflow
    assert "--candidate-run-attempt 1" in workflow
    assert "--write-receipt evidence-validation-receipt.json" in workflow
    assert "hash_vscode_release_evidence.py" in workflow
    assert '--expect "$EVIDENCE_INVENTORY_SHA256"' in workflow
    assert workflow.count("uses: actions/attest@") == 5
    assert "subject-path: attested-evidence/evidence/*" in workflow
    assert "subject-path: attested-evidence/receipt/*" in workflow
    assert "subject-path: attested-evidence/installed-live-provider/*" in workflow
    assert "subject-path: attested-evidence/untrusted-workspace/*" in workflow
    assert "subject-path: attested-evidence/remote-acceptance/*" in workflow
    assert 'test "$(find attested-evidence/evidence -maxdepth 1 -type f | wc -l)" -eq 9' in workflow
    assert "vscode-alysis-human-check-artifacts.zip" in workflow
    assert (
        'test "$(find attested-evidence/installed-live-provider -maxdepth 1 -type f | wc -l)" -eq 6'
        in workflow
    )
    assert "validate_vscode_installed_live_provider.py" in workflow
    assert "vscode-alysis-production-signoff.json" in workflow
    assert "--provider-origin-policy" in workflow
    assert "--installed-live-provider-run-id" in workflow
    assert "--installed-live-provider-run-attempt" in workflow
    assert "--untrusted-workspace-run-id" in workflow
    assert "--untrusted-workspace-run-attempt" in workflow
    assert "--remote-acceptance-run-id" in workflow
    assert "--remote-acceptance-wsl-run-attempt" in workflow
    assert "--remote-acceptance-remote-ssh-run-attempt" in workflow
    assert "vscode-remote-acceptance-attested-${{ github.sha }}" in workflow
    assert "fromJSON(inputs.workflow_run_attempts).remote_acceptance_wsl" in workflow
    assert "fromJSON(inputs.workflow_run_attempts).remote_acceptance_remote_ssh" in workflow
    assert (
        "-installed-live-provider-${{ inputs.installed_live_provider_run_id }}-attempt-" in workflow
    )
    assert "-${{ inputs.untrusted_workspace_run_id }}-attempt-" in workflow
    assert (
        "vscode-release-evidence-${{ github.sha }}-${{ inputs.candidate_run_id }}-${{ github.run_id }}-attempt-${{ github.run_attempt }}"
        in workflow
    )
    assert "retention-days: 90" in workflow


def test_evidence_workflow_actions_are_immutable_and_permissions_are_narrow() -> None:
    workflow = _workflow()
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)(?:\s+#\s*(.+))?$", workflow, re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference) for reference, _ in uses)
    assert all(comment for _, comment in uses)
    assert "contents: write" not in workflow
    assert "packages: write" not in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "artifact-metadata: write" in workflow
