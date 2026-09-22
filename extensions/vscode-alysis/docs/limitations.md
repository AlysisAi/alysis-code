# Known IDE v1 limitations

These limits must stay explicit and consistent in the README, changelog, release notes, and
Marketplace copy. Do not ship copy that implies any of them is solved.

## Summary list

- Forge swarm is review-only (per-task Keep/Discard, never auto-merged) and capability-gated.
- Broad Forge exec and `auto`/`fullaccess` execution are unavailable.
- Cancellation is cooperative checkpoint only, with no hard interrupt.
- MCP OAuth is unavailable in all remote VS Code extension hosts (SSH, containers, WSL, tunnels).
- Agent-controlled Managed Browser access is public-web only and unavailable to nested subagents.
- Direct IDE local testing is loopback-only.
- `hooks.watch` is unavailable.
- Raw trace output is unavailable.
- Arbitrary terminal start and interactive terminal streaming are unavailable.
- Setup/config terminal menus and inline API key setters are unavailable.

## Detail

- Real Forge Execute is limited to selected-task `review` mode. It requires Workspace Trust, trusted
  executable origin/secret forwarding, strict sandbox readiness, plan completeness, and host-managed
  scoped approvals. `no_log` and `max_steps` are explicit protocol params sourced from
  `alysis.forgeExecuteNoLog` and positive `alysis.forgeExecuteMaxSteps`; IDE Forge Execute
  rejects `subagents_enabled` params and keeps subagents disabled until worker lifecycle, sandbox,
  approval, and cancellation semantics are strong enough. `auto` and broad/fullaccess execution
  remain unsupported with no default experimental enablement.
- IDE Forge Swarm is the review-only Run Swarm console: parallel work is isolated, resumable, and
  presented for per-task Keep/Discard review. It never auto-merges or writes the base worktree;
  broad auto/fullaccess execution and direct merge-to-base remain CLI/server-only semantics.
- Chat and Forge sessions are routed independently. Unknown or unowned bridge events are ignored by
  the extension surface instead of being consumed by the wrong controller.
- Native side-by-side diff opens only when the backend provides old/new text or explicit artifact
  ids. Otherwise the extension opens the backend-provided unified diff preview read-only.
- `forge.executePreview` and `forge.execute` with `dry_run: true` are non-mutating previews. Real
  `forge.execute` starts a backend job only for review mode when backend capabilities and readiness
  checks allow it. `forge.cancel` is shown only when `features.forge.cancel.supported` is true.
- Active runtime cancellation is only reported as supported when the backend supports it. Current
  IDE Protocol v1 uses cooperative checkpoints, suppresses repeated cancel requests after
  `cancellation_requested`, and never marks a job cancelled without backend terminal-state
  reconciliation.
- Forge plan list/open is durable through persisted `.alysis/runs/<plan_id>/` artifacts, but the
  backend validates the persisted registry and rejects symlink escapes before artifact or diff
  access. Event replay is still a bounded in-memory bridge buffer, not durable history.
- Trace and terminal parity is intentionally narrow: `/trace` exposes backend-redacted bounded
  status/events/artifacts with explicit confirmation for `full`, and `/terminals` can list, show,
  kill, and clear existing managed background terminals. Arbitrary shell start, interactive PTY
  streaming, and raw unredacted trace output are not IDE v1 features.
