# Alysis Code VS Code extension — engineering documentation

The [top-level README](../README.md) is the Marketplace listing: what the extension does and how to
start using it. Everything here is for people working on the extension.

| Document | Contents |
| --- | --- |
| [architecture.md](architecture.md) | Capability inventory, IDE Protocol v1 surface, CLI compatibility and recovery, runtime status model, parity contracts. |
| [security-model.md](security-model.md) | Workspace Trust gates, credential handling and executable-origin rules, Restricted Mode, managed-browser trust boundaries. |
| [slash-commands.md](slash-commands.md) | Full slash command reference with trust and mutation semantics, plus the native `@alysis` chat participant. |
| [limitations.md](limitations.md) | The complete IDE v1 limits list that must stay consistent across README, changelog, release notes, and Marketplace copy. |
| [development.md](development.md) | Build, lint, test, Extension Host integration, packaging, and manual VSIX install. |
| [verification.md](verification.md) | Component and production-candidate verification, dogfood harness modes, evidence schemas, screenshot capture checklist. |
| [release-policy.md](release-policy.md) | Release channels, beta policy, and the Marketplace promotion runbook. |

Related repository-level documents:

- [`docs/ide_protocol.md`](../../../docs/ide_protocol.md) — protocol specification
- [`docs/generated/ide_cli_parity_matrix.json`](../../../docs/generated/ide_cli_parity_matrix.json) — IDE/CLI parity contract
- [`docs/vscode_extension_release_checklist.md`](../../../docs/vscode_extension_release_checklist.md) — required check IDs and manual smoke coverage
- [`docs/vscode_remote_acceptance.md`](../../../docs/vscode_remote_acceptance.md) — remote runner trust and cancellation recovery
- [`RELEASE_CHECKLIST.md`](../RELEASE_CHECKLIST.md) — extension release signoff
