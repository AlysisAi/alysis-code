# VS Code 0.3.0 public beta

The first Marketplace launch uses extension version `0.3.0` and the `beta` release channel.
The six supported targets are Windows, macOS, and Linux on x64 and ARM64. Users install the
Marketplace pre-release in VS Code; each target package includes its verified Alysis runtime.

## Required release sequence

1. Validate and tag the reviewed public source at the CLI version declared in `pyproject.toml`.
2. Configure the protected signing, provider QA, remote QA, evidence, asset, and Marketplace
   environments described in the [release checklist](vscode_extension_release_checklist.md).
3. Build and sign all six managed runtimes and VSIX packages from that exact source tag.
4. Pass clean installed-package tests on the minimum and current Stable VS Code builds, followed
   by real-provider, untrusted-workspace, WSL, and Remote-SSH acceptance on those exact packages.
5. Complete the candidate-bound human reports and production signoff, attest the evidence, and
   run the protected promotion workflow with `channel: beta` and confirmation `publish-beta`.
6. Verify every published target's downloaded bytes against the approved candidate.

The source-pinned single-maintainer approval policy allows `Perdikis10` (GitHub user ID
`190930654`) to fill engineering, security, and release roles and approve a self-initiated run.
Actual approvals are still required at every protected stage. Other reviewer configurations
retain independent approval requirements. Administrator bypass is disabled in both cases.

Development VSIX files, local mock tests, and component dogfood establish component behavior
only. They do not replace signed runtime, clean production install, or real-provider evidence.
No successful Marketplace publication is claimed by this source preparation record.
