# Releasing

This is the maintainer checklist for publishing Alysis Code packages and sandbox images.

## Version And Tag

1. Bump the package version in `pyproject.toml` and `src/alysis_code/__init__.py`.
2. Update `docs/CHANGELOG.md` with user-facing changes and known limitations.
3. Commit the release changes.
4. Create and push the release tag:

```bash
git tag v0.x.y
git push origin v0.x.y
```

## PyPI

Release tags build the wheel and source distribution. The publish job expects PyPI trusted
publishing to be configured for this repository and release workflow.

After the workflow finishes:

- Confirm the package page shows the expected version.
- Install the package in a clean environment.
- Run `alysis --help`.

## Sandbox Images

Sandbox images are published under:

```text
ghcr.io/alysisai/alysis-sandbox
```

Sandbox releases use an isolated Git tag namespace. Create a tag such as
`sandbox-v0.9.7`; ordinary Python or extension `v*` tags do not trigger the
container workflow.

The workflow first publishes source- and run-bound candidates under
`:candidate-<variant>-<full-sha>-<run-id>-<run-attempt>`. It scans and
smoke-tests both `linux/amd64` and `linux/arm64`, creates provenance, and signs
every candidate digest. Only after **all three variants** pass does the
promotion job create consumer tags:

- `:<variant>` for the moving variant tag, for example `:dev`
- `:<variant>-<sha12>` for the immutable per-commit tag
- `:<variant>-<sandbox-git-tag>` for release tags
- `:<sandbox-git-tag>` for the default variant

The default variant is `dev`. A manual run must target the repository default
branch and does not update moving tags unless `publish_moving_tags` is selected.
Candidate tags are not consumer release channels.
Promotion refuses to replace an existing per-commit or release tag with a
different digest; only the explicitly selected moving aliases may move.

Repository administrators must configure the `sandbox-release` GitHub
environment with required maintainer reviewers, self-review prevention, and
deployment-branch/tag restrictions for the default branch and `sandbox-v*`
release tags. The write-enabled promotion job is the only job attached to this
protected environment.

## Verify A Release Image

Pull the image:

```bash
docker pull ghcr.io/alysisai/alysis-sandbox:dev
```

For production use, prefer a digest-pinned image:

```bash
docker buildx imagetools inspect ghcr.io/alysisai/alysis-sandbox:dev
export ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE=ghcr.io/alysisai/alysis-sandbox@sha256:<digest>
```

Verify the signature and exact-source provenance for the immutable release digest. Replace the
placeholders with the tag and full source commit used by the retained workflow run:

```bash
cosign verify ghcr.io/alysisai/alysis-sandbox@<digest> \
  --certificate-identity 'https://github.com/AlysisAi/alysis-code/.github/workflows/sandbox-image.yml@refs/tags/<sandbox-tag>' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

gh attestation verify oci://ghcr.io/alysisai/alysis-sandbox@<digest> \
  --repo AlysisAi/alysis-code \
  --signer-workflow AlysisAi/alysis-code/.github/workflows/sandbox-image.yml \
  --source-ref refs/tags/<sandbox-tag> \
  --source-digest <full-source-sha> \
  --deny-self-hosted-runners \
  --predicate-type https://slsa.dev/provenance/v1
```

The protected workflow also resolves the exact `linux/amd64` and `linux/arm64` manifest digests,
extracts each BuildKit SPDX 2.3 SBOM, checks its recorded SHA-256, and verifies that the exact SBOM
semantically matches a source-bound `https://spdx.dev/Document/v2.3` attestation for that platform
manifest. Signature, scan, smoke, provenance, platform inventory, or SBOM predicate failures prevent
promotion; there is no best-effort release path.

## Troubleshooting

- GHCR rate limits: authenticate before repeated pulls.
- Package visibility: confirm the GHCR package is public before public launch.
- Vulnerability findings: review the advisory, decide whether it is exploitable, then patch or
  document an explicit temporary exception.
