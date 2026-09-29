from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "vscode-extension-untrusted-workspace.yml"
PACKAGE = ROOT / "extensions" / "vscode-alysis" / "package.json"
RUNNER = (
    ROOT
    / "extensions"
    / "vscode-alysis"
    / "test"
    / "integration"
    / "untrusted-production"
    / "runTest.ts"
)


def test_untrusted_workspace_gate_is_dispatch_only_and_exact_release_bound() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "pull_request:" not in workflow
    assert "candidate_run_id:" in workflow
    assert "vscode_test_version:" not in workflow
    assert 'VSCODE_TEST_VERSION: "1.90.0"' in workflow
    assert "inputs.vscode_test_version" not in workflow
    assert 'test "$GITHUB_REF" = "refs/tags/$RELEASE_TAG"' in workflow
    assert 'test "$(git rev-list -n 1 "$RELEASE_TAG")" = "$GITHUB_SHA"' in workflow
    assert ".github/workflows/managed-cli-vsix-release.yml" in workflow
    assert '.event == "workflow_dispatch"' in workflow
    assert '.conclusion == "success"' in workflow
    assert ".head_sha == $source_sha" in workflow
    assert ".head_branch == $release_tag" in workflow


def test_untrusted_workspace_gate_downloads_and_attests_exact_linux_candidate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "run-id: ${{ inputs.candidate_run_id }}" in workflow
    assert "name: vscode-alysis-linux-x64" in workflow
    assert workflow.count("gh attestation verify") == 2
    assert "https://slsa.dev/provenance/v1" in workflow
    assert "https://cyclonedx.org/bom" in workflow
    assert '--expected-predicate "$sbom"' in workflow
    assert "--deny-self-hosted-runners" in workflow
    assert "validate_production_dogfood_candidate_binding" in workflow
    assert "require_vscode_compatibility=True" in workflow
    assert "validate_vscode_untrusted_workspace_report.py" in workflow
    assert (
        "subject-path: untrusted-evidence/vscode-alysis-linux-x64.untrusted-workspace.json"
        in workflow
    )
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow
    assert "retention-days: 90" in workflow
    assert "ALYSIS_WORKFLOW_RUN_ID: ${{ github.run_id }}" in workflow
    assert "ALYSIS_WORKFLOW_RUN_ATTEMPT: ${{ github.run_attempt }}" in workflow
    assert '--workflow-run-id "$GITHUB_RUN_ID"' in workflow
    assert '--workflow-run-attempt "$GITHUB_RUN_ATTEMPT"' in workflow
    assert "-attempt-${{ github.run_attempt }}" in workflow


def test_untrusted_workspace_runner_has_a_dedicated_package_script() -> None:
    import json

    package = json.loads(PACKAGE.read_text(encoding="utf-8"))

    assert package["scripts"]["test:untrusted-production"] == (
        "npm run compile && node ./out/test/integration/untrusted-production/runTest.js"
    )


def test_untrusted_runner_does_not_use_standard_helper_or_disable_trust() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    launch_block = source.split("const launchArgs = [", 1)[1].split("];", 1)[0]

    assert "runTests" not in source
    assert "--disable-workspace-trust" not in launch_block
    assert "--extensionTestsPath=" in launch_block
    assert "workspacePath" in launch_block
    assert "userDataDir" in launch_block
    assert "readdirSync(userDataDir).length !== 0" in source
    assert "workspaceStorage" not in source
    assert "state.vscdb" not in source
    assert "requireExactAlysisInventory" in source
    assert "ALYSIS_MALICIOUS_WORKSPACE_CLI" in source
    assert 'APPROVED_UNTRUSTED_VSCODE_VERSION = "1.90.0"' in (
        RUNNER.parent.joinpath("contract.ts").read_text(encoding="utf-8")
    )
    assert "requestedVersion !== APPROVED_UNTRUSTED_VSCODE_VERSION" in source
    assert "ALYSIS_EXPECTED_VSCODE_VERSION: APPROVED_UNTRUSTED_VSCODE_VERSION" in source


def test_untrusted_workflow_actions_are_commit_pinned() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)(?:\s+#\s*(.+))?$", workflow, re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref) for ref, _comment in uses)
    assert all(comment for _ref, comment in uses)
