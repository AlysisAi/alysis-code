# Custom Tools

This package implements Alysis Code's single-file Python custom tools. A custom
tool is user- or workspace-provided code with a literal manifest and a
`run(args)` entrypoint.

## Contents

- `discovery.py` validates candidate tool files without importing them.
- `trust.py` records project-tool trust by workspace, relative path, and file
  hash.
- `session.py` builds the effective custom-tool catalog for a session.
- `runtime.py` executes trusted tools and applies declared capability checks.

## Scope

Project tools are not trusted by default. Editing or moving a trusted project
tool invalidates trust.

Discovery must remain import-free. Candidate code should not run while the host
is deciding whether the tool is valid or trusted.

Capability checks are not a complete sandbox. Treat custom tools as trusted
code and keep the documentation precise about that boundary.

## Development

Manifest, trust, and runtime changes should test valid and rejected tools, plus
the structured errors users will see.

## See Also

- [Custom tools](../../../docs/custom_tools.md)
- [Security model](../../../docs/security_model.md)
