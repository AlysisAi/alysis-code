# Sandbox input retention — 4 October 2026

Custodian: Perdikis10. Preserve every input and qualification receipt while any
supported image/release depends on it, including its rollback window. Actions
artifact expiry is not the retention policy. Do not delete or replace retained
assets when promoting a newer candidate; add a new manifest for changed inputs.
At retirement, check supported digest/tag references before a separate cleanup.

The private draft release `402967375` in `AlysisAi/alysis-code-internal`, tag name
`sandbox-inputs-20261004-82578f9d`, stores the following reviewed bundles. Its tag
names the initial trial; subsequent asset names and manifests bind their own exact
source commits. This is input custody, not a public image release or signing-key
backup. It contains no private keys.

## Retained inputs

- The two complete APK graphs: all 99 package archives for each architecture,
  original vendor-signed APKINDEX files, authentication keys, archive hashes,
  repository coordinates, and receipts.
- npm bootstrap, all 143 runtime dependency inputs, frozen lock, rebuilt npm
  output, and its functional/audit receipts.
- 93 additional hash-verified archives: Node and Go for both architectures,
  rustup installers, the Rust 1.91.1 manifest and minimal compiler/cargo/std
  distributions, and Python 3.12 Linux server/build wheels for both architectures.
  Platform selections, dependency versions, source URLs, sizes, and hashes are
  recorded; there are 65 selected runtime/build package entries per architecture.
- Complete OCI layouts for the pinned Wolfi base, uv helper and Dockerfile 1.7
  frontend, with all referenced index/manifest/config/layer blobs retained.
  Recursive validation checked 27, 15 and 78 blobs respectively against their
  content digests and descriptor sizes. No Docker credentials were used to fetch
  these public inputs.
- Relevant source at `a998c0b3`, `uv.lock`, `pyproject.toml`, export metadata,
  retrieval/verification scripts and member SHA-256 manifest.

The additional platform bundle is `platform-inputs-a998c0b3.zip`, 717,988,878 bytes,
SHA-256 `65460fdaab29a8b7efcf81f1c3086b55c7d545d9c0fcc88233beb502a2ef2362`.
GitHub's uploaded asset size and digest match. A separate SHA256SUMS asset is
retained. The source archive predates the offline Rust installer change; that
change's committed lock selects the same retained vendor bytes.

The OCI root digests are:

| Input | SHA-256 |
| --- | --- |
| Wolfi base | `b71f01d7fbb063a921577f355a82ea665e98c96e73c89a6ea0bcb773875b129c` |
| uv helper | `2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709` |
| Dockerfile 1.7 frontend | `a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e` |

The existing full-filesystem review is `filesystem-review-c1bae32d.zip`,
6,657,911 bytes, SHA-256
`2d95d22719c242f0e71a8e2e6371f9d405c1bb1a17988dc3a345ad1ad2a98f8f`.
The actual-default ACL review is `acl-boundary-a998c0b3.zip`, 6,655,329 bytes,
SHA-256 `edae6632021f97a3fb40aa019af3788c15d7601878406aa8050acbb455ee4596`.
Both remote asset sizes/digests were verified after upload.

## Release and recovery checks

Before production promotion, bind the exact source and final image digests to
these input manifests and retain the new production receipts. If a dependency
changes, preserve its new bytes before promotion. Recover missing upstream inputs
from the private archive, verify the outer digest and every member digest, then
use the original signed APK index, exact registry blob graph, or locked package
archive as appropriate. Preserve vendor source/licenses with the supported image.

Retention proves the selected bytes remain available; it does not prove a complete
offline rebuild. APK installation has been tested offline. Rust installation from
its hash-pinned local distribution passed all six jobs in private run `37217162237`
at `886466c64d13bd944387ba640ac4fdd3b44d60bb`. Complete
production build, public scans, signatures, SBOMs, attestations and protected
promotion remain required. The production Dockerfile now uses the qualified inputs.

Read-back receipts are in the original checkout's ignored launch evidence:
`retained-trial-inputs-release.json`, `platform-inputs-release-readback.json`,
`retained-platform-inputs/verification-summary.json`, `SHA256-MANIFEST.json`, and
the corresponding image-qualification summaries.
