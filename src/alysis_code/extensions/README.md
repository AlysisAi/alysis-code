# Extensions And Plugins

This package implements Alysis Code's plugin layer. Plugins can contribute skills,
custom tools, MCP servers, and hooks through a `alysis-plugin.toml` manifest.

## Contents

- `manifest.py` validates plugin metadata and component declarations.
- `install.py` handles install, uninstall, enable, and disable operations.
- `activation.py` computes the effective plugin set for a session.
- `state.py` and `paths.py` manage extension state and locations.
- `workspace_trust.py` protects project-level enable/disable overrides.
- `registry.py` and `registry.json` support curated registry lookup.

## Scope

Installing a plugin is an explicit trust decision. Session startup still filters
plugin-scoped components according to the effective activation state.

Project overrides are lower trust than user state. Non-interactive sessions
ignore untrusted project overrides rather than applying them silently.

## Development

Manifest schema changes should update validation, install behavior, and
`docs/plugins.md` in the same change.

## See Also

- [Plugins](../../../docs/plugins.md)
