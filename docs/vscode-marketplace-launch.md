# IDE release status

CLI source release: **0.14.4**. VS Code extension: **0.3.0**, planned Marketplace pre-release.

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
   are present. An independent encrypted manifest-key recovery copy and custody procedure still
   need confirmation. Configuration and secret presence do not prove successful signing.
2. Build and scan the pinned sandbox images for both architectures; source dependency updates
   alone are not security scan evidence.
3. Build six signed runtime/VSIX candidates from an immutable public source tag. Verify Windows
   Authenticode, Apple notarization, manifests, SBOMs, and GitHub attestations.
4. Install the exact candidates and complete live-provider, real WSL/Remote SSH, and untrusted
   workspace acceptance, with dedicated credentials and an approved provider spending limit.
5. Collect candidate-bound signoff and rollback evidence before explicit Marketplace promotion.

Production builds, attestations, and promotion use `AlysisAi/alysis-code`. The source tag must
match the CLI version; existing tags are never moved. The extension version is independent.
The `0.3.x` line selects `channel: beta` and publishes only through the Marketplace pre-release
channel after all evidence gates pass. A GitHub source release does not replace those gates.
