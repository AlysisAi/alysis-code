# Maintenance Scripts

This directory is for maintainer scripts that are not imported by the runtime
package during normal Alysis Code execution.

## Current Script

- `refresh_litellm_model_catalog.py` refreshes the bundled LiteLLM model pricing
  snapshot used by model metadata and packaging checks.
- `refresh_chatgpt_codex_model_catalog.py` sanitizes a reviewed, local ChatGPT
  Codex model-discovery response into the subscription-only capacity and
  capability fallback snapshot. It does not grant account model entitlement.

## Scope

Scripts may use network access or local tools. Review the script before running
it, and prefer explicit environment variables over persistent local state.

User-facing CLI behavior should live under `src/alysis_code/`, not in
this directory.

## See Also

- [Contributing](../.github/CONTRIBUTING.md)
- [Release process](../RELEASING.md)
