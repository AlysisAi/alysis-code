# IDE release status

CLI source version: **0.14.5**. VS Code extension: **0.3.0**, planned Marketplace pre-release.

This source release includes the VS Code integration, shared sidebar, JetBrains development
preview, and the CLI protocol and release tooling they require. It does not include production
signed VSIX packages and does not announce Marketplace availability. Component packages are
for development and testing only.

## Build and verify

Use [the extension checklist](vscode_extension_release_checklist.md) for component checks,
[signing operations](managed-cli-signing-operations.md) for protected signing configuration,
and [remote acceptance](vscode_remote_acceptance.md) for WSL and Remote SSH validation.
JetBrains preview setup and limitations are documented in
[its README](../extensions/jetbrains-alysis/README.md).

## Production gates still required

1. Verify the configured signing credentials in production. As of 29 September 2026, the
   dedicated Azure OIDC identity, profile-scoped signer role, seven protected Windows variables,
   and matching managed-runtime manifest key/fingerprint are configured. Apple credential names
   are present. An independent encrypted manifest-key recovery copy and custody
   procedure were verified on 4 October inside the approved Azure boundary, against
   the unchanged public key; audit delivery was also confirmed. Configuration,
   custody and secret presence do not prove successful production signing.
2. Sandbox qualification and publication passed on `a0ef8fff`, public run
   `37221977397`, including six native isolation/full-filesystem checks, zero
   HIGH/CRITICAL scan findings, signatures, and independently verified attestations.
   The native packaging fixes in 0.14.5 do not alter these published image bytes.
3. Build six signed runtime/VSIX candidates from a new immutable public source tag.
   Candidate `37276107624` from protected `v0.14.4` failed native packaging before
   manifest signing. Both Apple notarizations succeeded. The replacement version
   adds patched native Python, static OpenSSL where source builds require it,
   and platform-specific packaging repairs. Validate native unsigned builds before
   creating `v0.14.5`; never move `v0.14.4`. Windows Authenticode, Apple notarization,
   manifests, SBOMs, GitHub attestations, and clean installation must all pass.
4. Install the exact candidates and complete live-provider, real WSL/Remote SSH, and untrusted
   workspace acceptance, with dedicated credentials and an approved provider spending limit.
5. Collect candidate-bound signoff and rollback evidence before explicit Marketplace promotion.

Production builds, attestations, and promotion use `AlysisAi/alysis-code`. The source tag must
match the CLI version; existing tags are never moved. The extension version is independent.
The `0.3.x` line selects `channel: beta` and publishes only through the Marketplace pre-release
channel after all evidence gates pass. A GitHub source release does not replace those gates.
