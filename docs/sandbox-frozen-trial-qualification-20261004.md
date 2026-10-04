# Frozen sandbox trial qualification — 4 October 2026

All six jobs in [private run 37193916646](https://github.com/AlysisAi/alysis-code-internal/actions/runs/37193916646)
passed on source `82578f9dc46d8773bd84c61d2d61b9184b6c56cb`:
base, dev, and server on native amd64 and arm64. This qualifies the private
trial's build and scanner checks. The production Dockerfile now adopts this frozen
input path; public candidate scans, native boundary qualification, signatures,
attestations and protected promotion remain required before image distribution.

Follow-up [run 37209508490](https://github.com/AlysisAi/alysis-code-internal/actions/runs/37209508490)
passed all six jobs on `c1bae32d33c0bf483edb83aebc3f45db4f3ac0c1`, adding
complete Docker-export filesystem inventories. All six report/image/scan identities
match and all scans still have zero HIGH/CRITICAL findings. Across the six exports,
the reviewer inventories 140,353 file/link entries, hashes regular file contents,
and inspects 3,168 ELF files/hardlinks.
It does not extract or execute archive members. Seven boundary tests pass.

The initial inspector run `37209135983` passed the two base jobs but failed the
other four when Go's ELF test fixtures retained `.dynsym` and `.dynamic` names as
empty `SHT_NOBITS` sections. The correction classifies sections by their actual
ELF type and preserves the named-section metadata. The known fixtures appear in
the successful reports; no malformed binary is silently skipped.

## Verified results

- Each image built, ran the required tools as UID 1000, and passed restricted
  execution with no network, no effective capabilities, no-new-privileges, a
  read-only home, and a writable temporary directory.
- All six exact image IDs match their respective Trivy reports. Each report has
  zero HIGH/CRITICAL findings, with unfixed findings included and no exceptions.
  Scanner/database identity and installed package inventories are retained.
- Dev/server images exercised the rebuilt npm/npx package, offline local package
  operations, dependency bins, and mocked HTTP behavior. Server CLI smoke passed.
- Each architecture has 99 exact APK versions. All six installed graphs match
  the committed architecture-specific lock. The builder verifies every archive's
  complete SHA-256, verifies the original vendor-signed APKINDEX, and installs
  with native APK checksum authentication in a network-disabled build step.
- Both original signed indexes, public verification keys, and all 198 APK archives
  were downloaded from the CI evidence. Every archive hash matches its committed
  manifest; both index hashes match the run receipts. No APK signature or trust
  check is bypassed. These packages are authenticated through the signed index,
  rather than individual archive signatures.

The current versions include Python `3.12.15-r0`, pip `26.2.1-r2`, npm
`11.20.0-alysis.1`, and locked application urllib3 `2.8.0`. The Python release
includes the [tarfile fix](https://mail.python.org/archives/list/security-announce%40python.org/thread/EFJWGAZJA56AKSBR2WHMHQZO7RRLZPRH/).
Wolfi's pip rebuild incorporates [urllib3 2.8.0](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77).
The first patched six-image pass was `37193250487`; the final run above additionally
verifies complete transitive pinning, retained authentication inputs, and offline
installation.

## Remaining production review

A change of distribution does not by itself close the earlier Debian findings.
The earlier cross-distribution review and this follow-up must accompany production
adoption; the zero-finding scan is not a claim that every upstream issue is fixed.

- The [Debian ncurses record](https://security-tracker.debian.org/tracker/CVE-2025-69720)
  identifies the affected `infocmp` range as before the 2025-12-13 fix. The locked
  ncurses `6.6.20260926-r0` is newer, supporting the version-based disposition.
- ELF import inspection of the authenticated APK contents found one remaining
  legacy ACL caller on each architecture: `usr/bin/coreutils` imports
  `acl_set_file` and `acl_delete_def_file`. The [upstream ACL announcement](https://www.openwall.com/lists/oss-security/2026/06/29/1)
  says those legacy path APIs retain their symlink-following behavior. This import
  evidence does not establish exploitability, but it prevents treating libacl
  2.4.0 alone as a universal fix. The authenticated coreutils recipe pins source
  `c0f8514d989184921d9b12a4d103a7b23abc5af8`, whose gnulib submodule is
  `106e9b2384d08a1696fcbd40cbab52237943f208`. Its
  [permission setter](https://github.com/coreutils/gnulib/blob/106e9b2384d08a1696fcbd40cbab52237943f208/lib/set-permissions.c)
  uses `acl_set_fd` when given a descriptor, but retains path-based calls for
  fallback access ACLs and directory default ACLs. Coreutils'
  [copy implementation](https://github.com/coreutils/coreutils/blob/c0f8514d989184921d9b12a4d103a7b23abc5af8/src/copy.c)
  passes `-1` descriptors on its non-regular-file preservation path. This is
  concrete caller evidence. The [actual execution-policy review](sandbox-acl-boundary-review-20261004.md)
  now closes applicability within the application-controlled Docker boundary;
  all six images pass both UID 1000 and UID zero checks in run `37215707465`.
  No exception, caller-remediation claim, or universal-fix claim is recorded.
- The same inspection identifies Expat consumers in Git's `git-http-push` and
  Python's `pyexpat`. Upstream [issue 1160](https://github.com/libexpat/libexpat/issues/1160)
  still lists unresolved non-public issues. The additional disclosed
  [CVE-2025-66382](https://www.cve.org/CVERecord?id=CVE-2025-66382) has a CNA score of
  2.9/LOW in the retained CVE record (updated 14 July 2026). It did not occur in the
  original blocking Debian report, whose four Expat findings were CVE-2026-66046,
  CVE-2026-76956, CVE-2026-76957 and CVE-2026-93990; those have fixes in the selected
  2.8.5 release. Upstream and Debian still describe 66382 as unfixed, so the CVE
  record's narrower affected-version range is not used to claim remediation.
  This is a documented residual LOW issue, not a new HIGH/CRITICAL release blocker
  or a severity-feed substitution. Undisclosed reports have no assumed severity
  or invented applicability result. No scan exception was added.
- The [kernel-header review](sandbox-kernel-header-review-20261004.json) records
  individual dispositions for all 89 original kernel findings. The authenticated
  archives contain 1,025 amd64 and 996 arm64 `.h` files, a header-checking script,
  and package metadata; neither archive contains ELF code. Eighty-four CVE records
  name internal kernel files; five older descriptions identify runtime kernel
  behavior. One filename overlap, `linux/vdpa.h`, was examined against the actual
  [upstream fix](https://github.com/torvalds/linux/commit/85bb534ff12aab6916058897b39c748940a7a4c6):
  the exported UAPI header is different from the affected internal kernel header.
  Complete final-image inventories now find no conventional kernel-image or
  module paths in any of the six exports. Their build hosts all report Ubuntu
  24.04.5, kernel `6.17.0-1022-azure`, Docker 28.0.4, AppArmor and default seccomp.
  Ubuntu's critical [CVE-2026-43185 record](https://ubuntu.com/security/CVE-2026-43185)
  lists the noble `linux-azure-6.17` fix at `6.17.0-1021.21~24.04.1`, preceding the
  observed ABI. This supports that specific build-host finding, not a blanket
  statement about all host CVEs or future deployment hosts. Header versions do not
  establish host patch state. Dynamic section inspection also does not prove the
  absence of statically linked code or undisclosed defects.
- The full-image review confirms the same single legacy ACL caller and two Expat
  consumers on all six images. Coreutils is not setuid/setgid. Six account-management
  executables do carry such bits; the application's Docker runner always drops all
  capabilities and enables no-new-privileges. However, unlike the strict trial,
  its default root filesystem is writable and resource limits are optional; on
  POSIX it adopts the host UID, including zero if the application runs as root.
  Do not infer a universal non-root/read-only/resource-limited execution policy
  from the trial. Follow-up actual-default tests exercise the writable-rootfs
  profile without optional resource limits and establish the scoped ACL boundary
  described above. Privileged/manual execution remains outside that decision.
- Production must use the frozen input path and retain its input evidence for the
  supported release lifetime, then pass the public sandbox workflow's scans,
  runtime checks, signatures, SBOMs, attestations, and protected promotion.
  Private Actions artifacts expire after 14 days. The APK/npm inputs and current
  qualification receipts now also exist in private draft release `402967375`
  (`sandbox-inputs-20261004-82578f9d`, targeting the trial source). All six remote
  asset sizes and SHA-256 digests were verified against local files; they total
  548,484,630 bytes. This closes the immediate Actions-expiry gap for those inputs,
  but not complete toolchain/base-image retention or production lifetime policy.

## Evidence

Under the original checkout's ignored `qa_reports/marketplace-launch-20261004/`:

- `sandbox-trial-37193916646/verification-summary.json` binds all six image IDs,
  scan results, complete package graphs, archive hashes, and signed index hashes.
- The adjacent run/artifact receipts and six artifact directories retain the
  actual inputs and runtime evidence.
- `sandbox-patch-provenance.json` records upstream recipe blob identities.
- `sandbox-os-elf-review.json` records 457 amd64 and 453 arm64 ELF inspections,
  relevant imports, binary hashes, and header paths. Its scope is OS package
  contents, not all manually installed toolchains or the complete final image.
- `coreutils-package-recipe.yaml` retains the authenticated archive's embedded
  build recipe. `coreutils-gnulib-binding.json`, `coreutils-copy.c`, and
  `gnulib-set-permissions.c` retain the exact source binding and reviewed callers.
- `kernel-cve-review.json`, `kernel-cve-records/`, `kernel-header-comparison.json`,
  `vdpa-kernel-fix.json`, and the retained vDPA headers support the committed
  per-CVE header-package review. CVE records are fixed to cvelist commit
  `808914b893dc0f57b5c2014da863e9e476381eed` and individually SHA-256 recorded.
- `retained-trial-inputs/retention-manifest.json` records every archived input and
  member digest. `retained-trial-inputs-release.json` retains the private draft
  release's read-back metadata and server-reported asset digests. No signing
  private key is included in these explicitly selected input/evidence bundles.
- `sandbox-filesystem-37209508490/verification-summary.json` binds the six full
  filesystem reports to their exact image IDs, Trivy reports and build-host facts.
  `filesystem-review-c1bae32d.zip` retains those reports, the successful run receipt,
  the two reviewed CNA records and the independent receipt checker. The private
  draft release also retains this 6,657,911-byte bundle and its SHA256SUMS file.
  GitHub's asset digest matches local SHA-256
  `2d95d22719c242f0e71a8e2e6371f9d405c1bb1a17988dc3a345ad1ad2a98f8f`.

The APK-lock and npm-rebuild boundary suites passed 17 tests. Workflow lint,
Python lint/format checks, and whitespace validation also passed.
