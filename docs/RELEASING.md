# Releasing Alysis Code

This document is for maintainers publishing the Python package or sandbox images. Release
credentials are managed through protected GitHub environments; they must not be stored in the
repository or passed on the command line.

## Python package

1. Start from a clean commit on `main` with all required checks passing.
2. Update the version in `pyproject.toml` and `src/alysis_code/__init__.py`.
3. Move user-facing entries from `Unreleased` into a dated section in `docs/CHANGELOG.md`.
4. Run the release checks locally:

   ```bash
   uv sync --frozen --extra dev
   uv run --frozen --no-sync ruff check .
   uv run --frozen --no-sync ruff format --check .
   uv run --frozen --no-sync pytest -q
   uv build --clear --no-build-isolation --no-sources --out-dir dist/python
   uv run --frozen --no-sync python scripts/release/validate_python_distributions.py dist/python
   ```

5. Commit the release metadata and obtain review.
6. Create and push the version tag:

   ```bash
   VERSION=0.14.0
   git tag "v${VERSION}"
   git push origin "v${VERSION}"
   ```

The tag-triggered `release` workflow validates that the tag, package version, source commit, and
default branch agree. It then tests and builds the source distribution and wheel, creates an SBOM
and provenance attestations, publishes through PyPI trusted publishing, and creates the GitHub
release.

After publication:

- confirm the GitHub Actions run completed successfully;
- verify the version and files on PyPI;
- install the wheel in a clean environment; and
- run `alysis-code --version` and `alysis-code --help`.

Never move or reuse a published version tag. If a release is defective, fix the problem and publish
a new patch version. Yank a PyPI release only when leaving it available would harm users.

## Sandbox images

Sandbox images are published from tags named `sandbox-vX.Y.Z`. The `sandbox-image` workflow builds,
scans, signs, attests, and smoke-tests each supported image variant before promotion.

Before tagging, confirm that the protected `sandbox-release` environment has required reviewers,
self-review prevention, and deployment restrictions for `main` and `sandbox-v*` tags.
The workflow checks that reviewers and self-review prevention are configured before building.

Create and push the tag:

```bash
VERSION=0.14.0
git tag "sandbox-v${VERSION}"
git push origin "sandbox-v${VERSION}"
```

After the workflow succeeds, inspect the published manifest and prefer digest-pinned references in
production:

```bash
docker buildx imagetools inspect ghcr.io/alysisai/alysis-sandbox:dev
```

The workflow file is the authoritative description of image variants, signing, provenance, SBOM,
and promotion checks.

### First publication and missing-image recovery

The CLI and sandbox have separate releases. Updating the image name in source or merging a PR
does not publish an image. PR jobs build locally without publishing. Rerunning an old Sylliptor
workflow also uses that run's old source and image name.

After merging reviewed changes into the public repository's default branch and configuring
`sandbox-release`, start a new run with moving tags enabled:

```bash
gh workflow run sandbox-image.yml --repo AlysisAi/alysis-code --ref main \
  -f default_variant=dev -f publish_moving_tags=true
```

Without `publish_moving_tags=true`, a manual run does not create or update the `:base`, `:dev`,
`:server`, and `:latest` references used by normal installations. A `sandbox-v*` tag release does
update those references.

For the first publication, the package owner must check **Packages > alysis-sandbox > Package
settings**, set visibility to **Public**, and grant this repository Actions access. New GHCR
packages default to private; a public source repository does not establish anonymous package
access. See [GitHub's package visibility guide](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

The workflow checks anonymous candidate access before changing consumer tags. If that check
fails because the new package is private, correct its visibility and rerun the failed jobs.
Resolve scan or smoke-test failures before approving the protected promotion job. A release is
successful only after every promoted reference passes anonymous pulls on both architectures.

Reproduce a fresh user's download without changing the operator's Docker login:

```bash
bash scripts/sandbox/check-public-images.sh --pull \
  ghcr.io/alysisai/alysis-sandbox:dev \
  ghcr.io/alysisai/alysis-sandbox:server
alysis sandbox doctor --smoke
```

The script uses an empty temporary Docker credential configuration. Without `--pull`, it checks
public manifests and platform support without downloading image layers. Do not close an image
availability issue based only on a successful build or an authenticated pull.

### Image security maintenance

Refresh affected version pins and their checksums together with the Python image digest and
Debian snapshot. Upgrade inherited Debian packages against that snapshot as well as installing
new packages. Python and `venv` already come from the Python base image. Node's bundled npm can
lag security fixes, so npm has its own version and verified distribution checksum.

HIGH/CRITICAL findings remain blocking, including unfixed findings. Review retained reports from
both architectures; a reduction in findings is not a passing scan. Exceptions require evidence
that the vulnerable code does not apply to the image, with the scope and rationale documented.
Do not blanket-ignore unfixed findings or remove scanner metadata to make an image pass.

The Docker image's Bubblewrap package receives updates through the Debian snapshot. Native Linux
Bubblewrap installations use the host package and receive updates through the host package manager.

## Failed releases

- Do not rerun publication from a different commit under the same tag.
- Do not bypass a failing validation, scan, signature, or provenance check.
- Keep failed workflow logs and artifacts private if they contain operational details.
- Correct the source, increment the version when necessary, and create a new reviewed tag.
