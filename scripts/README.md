# Maintenance scripts

This directory contains repository maintenance, quality-assurance, release, and benchmark helpers.
They are not imported by Alysis Code during normal use.

## Common tasks

- `refresh_litellm_model_catalog.py` updates the bundled LiteLLM model metadata snapshot.
- `refresh_chatgpt_codex_model_catalog.py` creates a sanitized fallback snapshot from a reviewed
  model-discovery response.
- `build_benchmark_wheel.sh` builds a provenance-stamped wheel for benchmark adapters in a unique
  `dist/benchmark.*` directory, then restores the original local build metadata.
- `qa/` contains dependency, repository-hygiene, protocol, and mock-runtime checks.
- `release/` contains package validation, dependency auditing, and attestation checks used by
  GitHub Actions.

Run a script with `--help` when available and inspect it before granting network access or release
credentials. Scripts that produce reports or build artifacts should write only to ignored output
directories.

User-facing commands belong under `src/alysis_code/`, not in this directory.

See [Contributing](../.github/CONTRIBUTING.md) and the
[release process](../docs/RELEASING.md).
