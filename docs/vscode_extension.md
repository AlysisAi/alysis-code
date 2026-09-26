# VS Code Extension Architecture and Integration

This document describes the architecture, backend contracts, and release gates for the
extension under `extensions/vscode-alysis`. It has an Alysis Code Cockpit webview, structured Forge Plan UI,
non-mutating Forge Execute Preview UI, and a first Forge Execute v1 review-mode path when the
backend reports prerequisites are ready.

## Direction

The VS Code extension should be a native IDE client for Alysis Code, not a terminal wrapper around
`alysis chat`. The extension should keep editor concerns in VS Code and agent/runtime concerns
inside the installed Alysis Code package.

The intended shape is:

- a TypeScript VS Code extension as the IDE host
- a local Alysis Code IDE bridge/protocol owned by the Python package
- explicit version negotiation between extension and bridge
- structured event streaming from the [IDE protocol event model](ide_protocol.md#event-format)
- host-owned approvals, cancellation, and artifact/diff routing
- no model API keys in prompts, logs, command lines, or extension-visible plaintext

The installed command remains `alysis`, and the package remains `alysis-code`.

## Backend Parity Contract

The extension parity source of truth is
[`docs/generated/ide_cli_parity_matrix.json`](generated/ide_cli_parity_matrix.json). The generated final burn-down is
[`docs/generated/ide_cli_parity_burndown.md`](generated/ide_cli_parity_burndown.md), with a
Forge-specific release-review projection in
[`docs/generated/ide_forge_parity_burndown.md`](generated/ide_forge_parity_burndown.md). These
files are intentionally machine-readable or generated from machine-readable data: Markdown prose
alone is not enough to decide whether a CLI feature should appear in VS Code.

The parity validator:

```bash
python scripts/qa/check_ide_cli_parity.py
```

statically checks Python Typer command registrations, CLI chat and Forge slash commands, IDE bridge
methods, package command contributions, and the extension slash registry. It fails when a new CLI
command, IDE method, package command, or extension slash command appears without a parity
classification. Documentation coverage is enforced for IDE methods and extension slash commands.
For VS Code package commands, the JSON contract also records route metadata for command-palette
commands, hidden Sessions/Manage tree context commands, Manage tree title commands, backend action
groups, backend actions, and documented command handlers. Hidden context commands are not allowed to
exist only in `package.json`: they must map to valid `BackendActionMetadata` entries or an explicit
handler, and commands marked `implemented_in_extension` must have a registered route.

IDE method params are also tracked in
[`docs/generated/ide_protocol_methods.json`](generated/ide_protocol_methods.json). Extension tests
validate `BackendActionMetadata` and `BackendActionController` against that fixture: every required
method must exist in the protocol contract, mutating actions must be Workspace-Trust gated or carry
an explicit exemption rationale, slash aliases must be unique, and collected params must satisfy the
Python method schema. Bugs like stale `profile.rename` params or mutually exclusive
`skill.validate` params are contract failures, not manual-review-only issues.

The policy boundary is explicit: the extension must not become a terminal-output scraper. Terminal
setup/config menus stay out of the IDE path until they have typed metadata or native VS Code UI.
The Alysis Code application-level `server start` command stays CLI-only while its
lifecycle/auth/port exposure concerns remain unresolved; this is distinct from the owner-scoped
`mcp.server.*` lifecycle below.
Full Forge swarm and terminal Forge `/execute plan` stay blocked until worker lifecycle,
cancellation, sandbox, and approval UX are strong enough. `hooks watch` needs a streaming/lifecycle
contract instead of a simple request/response method. Secret mutation flows must not accept inline
secret protocol params. `/trace` and `/terminals` are typed IDE workflows only for backend-redacted,
bounded trace disclosure and existing managed-terminal inspection/lifecycle; raw trace disclosure,
arbitrary terminal creation, and interactive PTY streaming remain blocked. `/model-info`,
`/paste-image`, and `/subagents status|on|off` are typed IDE workflows; they do not reuse the
terminal slash parser or send image binaries through JSONL.

## Component and Production-Candidate Evidence

`bash scripts/qa/check_vscode_extension_release_candidate.sh` and the matching GitHub workflow
validate the generic extension component. Their VSIX has no signed managed runtime, is explicitly
not-for-release, and can never produce `release_valid: true`. Production candidates come only from
`.github/workflows/managed-cli-vsix-release.yml`: one target-specific package per platform, built
from an immutable tag, with native signing policy, signed manifest, executable/final-VSIX
provenance and SBOM attestations, and isolated installed-production health checks. Public beta
signoff also requires completed manual real-provider evidence, installed live-provider evidence for
all six targets, a real untrusted-workspace Extension Host run, and real WSL plus Remote-SSH runs.
The installed live-provider gate consumes its protected API key only after the base URL matches a
separate protected exact HTTPS origin and resolves publicly. Its redacted report binds every live
runtime identity field to the exact production-dogfood JSON, records only the normalized provider
origin and bounded provider/model identifiers, and proves profile reuse with persisted non-secret
state across two Extension Host launches.
Generate
the checklist with
`python scripts/qa/vscode_extension_dogfood.py --mode manual-real-provider --vsix-path
/absolute/path/to/vscode-alysis-<target>.vsix --package-mode require`,
then validate the completed local JSON report with
`python scripts/qa/validate_vscode_manual_provider_report.py <report.json> <target.vsix> <production-dogfood.json>`.
The
validator binds the report to the exact signed candidate bytes. A generated
`manual_steps_written` report is only a placeholder, HTTPS report URLs are rejected by CI, and the
validator requires schema-v5 `completed-manual-report`, immutable candidate/release/host identity,
the exact target VSIX and production-dogfood bytes, managed-runtime hash/signature/install evidence,
timestamped per-check receipts for every current `required_checks` ID, and secret-free contents.
Use `docs/vscode_extension_production_signoff_template.md` for the final release-manager signoff
record. The signoff must include the strict component gate, target production-dogfood summary path,
manual real-provider report path, OS smoke evidence, accepted known limitations, no P0/P1 blockers,
and rollback plan. The dispatch-only `vscode-extension-evidence` workflow verifies the exact source
tag, candidate and acceptance run identities, original attestations, nine reviewed files
(including the closed production signoff/rollback receipt), six
redacted installed-provider reports, one actual-untrusted report, and the two real-remote reports.
It re-attests one immutable candidate-bound evidence bundle. The separate protected promotion
workflow re-downloads and revalidates that frozen bundle before reconciling release assets or using
the Marketplace credential; generic component evidence is never accepted by either workflow.

## Current Extension

The extension in `extensions/vscode-alysis` currently owns the first chat/run IDE flow:

- extension metadata, settings, command registrations, and local VSIX packaging
- a native Managed Browser surface that is fail-closed on the complete managed-browser security
  capability and owner-scoped. Normal `public` sessions are shared with the top-level agent. A
  separately confirmed `public_loopback` start is direct-user-only and agent-isolated, allows
  public plus loopback destinations, and denies LAN/link-local. Both paths offer bounded snapshots
  and diagnostics, verified chunked PNG previews, native Save As, and confirmed exact-session
  cleanup
- per-domain capability gating: the extension/CLI compatibility contract derives independent feature
  ids per management domain (config, profiles, sessions, doctor, sandbox, tools, skills, hooks,
  conventions, mcp, ext, update, report) from `getCapabilities`, so a missing domain does not disable
  an unrelated one. Reserved ids resolve to the real bridge methods — `forgeReviewGate` →
  `forge.review`, `assets` → `forge.assets.list`/`show`, `providersProfiles` → `config.*` +
  `profile.*` — while `forge.pr`, `forge.swarm`, and fullaccess mode stay gated. Diff apply/discard
  has no in-cockpit control at all: VS Code Source Control owns staging and reverting. These gates are
  driven by the LIVE bridge, not a stale snapshot: the bridge client emits a `health` event whenever its
  `initialize` handshake completes or drops, and the cockpit runtime recomputes the compatibility
  snapshot from the freshest capabilities on that event (and on every status-bar change), republishing
  in place. So a healthy connected CLI un-gates the Manage tree / provider pill / runtime drawer with no
  window reload — the drawer's CLI-health / protocol / bridge rows agree with the status bar — while a
  genuinely-missing capability still gates with the specific missing IDE bridge method named, and the
  permanently-gated set stays gated regardless of what the bridge advertises. Manage Refresh re-reads
  live health before re-rendering
- a Forge review gate (`forge.review`): a review-only "Review changes" action over the working-tree
  changes that renders the decision/confidence/issue counts/redacted summary and links the review
  artifacts; it never commits, merges, or pushes. The diffs surface exposes only View diff and a
  single Source Control pointer — there are no in-cockpit Keep/Discard controls. A read-first Forge
  assets surface (`forge.assets.list`/`show`) lists and views plan assets; asset mutations are
  capability- and Workspace-Trust gated
- an incremental, keyed transcript renderer: item nodes are reused across publishes (the streaming
  message patches in place rather than rebuilding #items per delta), streaming publishes are
  debounced to frame cadence, chat history is capped at the 200 newest items, and the view sticks to
  the bottom only when the reader is already there. The transcript tablists are keyboard-navigable
  (roving tabindex + arrow keys) with a dedicated polite live region and an `aria-busy` streaming
  signal
- discoverability + first-run entry points: a status-bar item and view-title buttons that open the
  cockpit or create a new Cockpit session (bridge health stays reachable from the status-bar
  tooltip), a Sessions `viewsWelcome` (Open Cockpit / Set up / Locate CLI), and a "Get started with
  Alysis Code" walkthrough. The
  **Locate Alysis Code CLI** command auto-detects the CLI (PATH, the active virtualenv, the configured
  Python interpreter directory, and common install dirs), validates a chosen binary with `--version`
  + `ide-bridge health` without spawning an untrusted workspace-local binary, and saves
  `alysis.cliPath` (workspace or global) before reconnecting
- CLI discovery through configured `alysis.cliPath` or `alysis` on `PATH`, with executable
  origin resolution before process launch and API-key forwarding
- `alysis --version`, `alysis ide-bridge health`, and `alysis sandbox doctor --smoke`
  clients
- protocol health validation for version, transport, and baseline Cockpit methods, plus an
  `initialize` handshake against the live stdio bridge before session creation. Feature methods are
  evaluated separately so an old CLI can keep the Cockpit open while chat, Forge Plan, Forge
  Execute Preview/Review, diffs, or artifacts are disabled with explicit upgrade guidance.
- stdio bridge startup, JSONL request/response correlation, explicit session/job-owned structured
  event routing, safe stderr handling, and bridge shutdown on extension deactivate
- correlated semantic lifecycle for model-managed chat helpers. `subagent_state_changed` updates one
  calm activity row per child while nested tool events retain worker attribution; these delegates
  remain owned/cancelled by the parent job. Independently resumable background workers use the
  revision-fenced Forge swarm recovery, cancellation, review, and usage surface
- secure chat webview with crypto-backed nonce, strict CSP, packaged local script/style assets, and
  text-only rendering of model/tool/workspace output
- Alysis Code Cockpit webview as the primary workflow surface, redesigned in a black-&-white theme
  (status encoded by glyph/shape/weight, not hue; supports dark and light). It is chat-first: the
  header keeps a single compact row of mode / model / trust chips that stays one horizontal row even at
  a docked side-panel width (it scrolls if it ever overflows, never wrapping into full-width bars), with
  the runtime Details drawer behind the trust chip. The header carries one wordmark and one canonical
  status surface — the connection dot plus that runtime chip; the bridge/run state is not repeated as
  meta text, and the compact header wordmark steps aside in the empty state so only the large onboarding
  wordmark shows. The decorative mascot docks as a small (~24px) glyph aligned to the header centerline
  (aria-hidden, reduced-motion static fallback), not a floating sprite. There is also a session-mode
  pill (readonly/review/auto live; `fullaccess` is presented non-selectable
  under a "Not available" divider — even when the `modesFullaccess` capability is advertised and the
  workspace is trusted the host still refuses it, and when bridge health reports a security-model
  block the row says "Blocked pending the security model" with the bridge rationale in the tooltip
  rather than a control that errors on click). The model pill is sourced from `session.modelInfo`
  after a live session exists, shows the active model plus profile, prefers the active provider
  profile model before the static `default` fallback on first open, and uses `session.setModel` for
  real session model switches when the bridge advertises it; older bridges route to Configure
  Provider with explicit copy instead of pretending model switching is available. The composer has a Chat | Forge mode switch; in Forge, the
  plan card folds in a "What Execute Plan will do" readiness panel and an Execute Plan action that is
  review-only and writes only to the working tree (never commits). The Execute tab reads as a confirm:
  readiness/scope/approvals stay at the top and protocol internals (real-execution support, sandbox
  diagnostic, active cancellation) sit behind a collapsed Details disclosure. After execution a
  diff-review card opens each changed file in the native VS Code diff via View diff and carries a
  single Source Control pointer — there are no per-row Keep/Discard controls (staging/reverting is
  owned by VS Code Source Control). The Forge/details workspace is a right-side tab panel (Plan,
  Execute, Diffs, Artifacts, Diagnostics, and a **More** tab that surfaces live Forge assets with a
  count badge alongside a "Not available yet" group of the genuinely-gated teases — Open as PR and
  Run as swarm) that collapses until a plan, preview,
  artifact/diff, approval, or recovery state needs inspection. Empty Plan/Diffs/Artifacts/Task-detail
  surfaces share one calm empty-state component (a decorative glyph, one line, and at most one CTA). The
  chat-empty onboarding is anchored a comfortable distance below the header (not vertically centered in
  the whole timeline), lays its four starters out as an even 2×2 grid, and the Chat | Forge mode switch
  is visually tied to the input it controls.
  Detailed provider, Workspace Trust, CLI Origin, protocol, and sandbox-doctor state live in
  Diagnostics instead of crowding the header. The cockpit restores on window reload (via a
  `WebviewPanelSerializer`) instead of leaving a blank editor group. The shell holds together at a
  docked side-panel width: the runtime Details drawer and management-action result cards are
  height-bounded and scroll internally (oversized results collapse behind a "Show details" toggle, so
  no single card or the drawer can ever push the composer off the bottom — the timeline is the only
  region that flexes and the composer stays pinned as the shell's last row), every scroll region
  reserves a stable scrollbar gutter, and long text/labels keep horizontal padding so nothing clips
  under the scrollbar. The Activity Bar Sessions, Forge
  Plan, and Artifacts views remain available as focused secondary views.
- Alysis Code chat webview slash commands routed by an extension-side `SlashCommandRouter` rather
  than the terminal CLI parser or raw `chat.send` text. The supported v1 command set is `/help`,
  `/permissions readonly`, `/permissions review`, `/permissions auto`, `/forge plan <instruction>`,
  `/execute preview`, `/execute plan`, `/plans`, `/open plan [plan_id]`, `/diffs`, `/artifacts`,
  `/cancel`, `/doctor`, `/config`, `/config set <key=value>`, `/update [cached|online]`,
  `/usage`, `/history <query>`, `/context`, `/ctx`, `/compact [focus]`, `/resume <session_id>`,
  `/model-info [model]`, `/subagents status|on|off`, `/permissions`, `/model <model>`, `/persona [name]`
  (bare opens the persona picker; with a name it switches directly, and the CLI clamp keeps every
  switch non-escalating), `/stream on|off`,
  `/cd <path>`, `/pwd`, `/status`, `/clear`,
  `/image [path]`, `/paste-image [path]`, `/images`, `/clear-images`, `/skills`, `/skill <name>`, `/tools`, `/hooks`,
  `/mcp`, `/assets`, `/asset list`, `/asset show <asset_id>`, `/asset add <path>`,
  `/asset delete <asset_id>`, `/asset edit <asset_id>`, `/asset refresh <asset_id>`,
  `/asset cancel-pending`, `/asset check`, `/asset prune`,
  `/forge plan show [plan_id]`, `/forge show [plan_id]`, `/plan show [plan_id]`, `/forge plan state`,
  `/forge plan validate`, `/forge plan regenerate [focus|instruction]`,
  `/plan regenerate [focus|instruction]`, `/assistant [instruction]`, `/goal [goal]`,
  `/task <task_id> [show|status <status>|title <title>|body <text>]`,
  `/forge review <task_id>`, `/review <task_id>`, `/profiles`, `/profile <name>`, `/profile use <name>`, and
  `/report [feedback]` / `/feedback [feedback]`. Unknown slash commands fail closed with help and
  are not sent to the model.
- slash autocomplete and quick actions in the webview. Up/down moves suggestions, Tab or Right
  Arrow accepts the highlighted suggestion, Enter submits complete commands or commands that already
  include arguments, Enter only applies a suggestion for partial argument-taking commands such as
  `/pla`, Shift+Enter inserts a newline, Escape closes suggestions, and all command output remains
  text-rendered under the existing CSP.
- a simplified composer with one textarea, one Send button, primary Plan / Execute Preview /
  Execute Review actions, and a More menu for Plans, Diffs, Artifacts, Doctor, Config, Cancel, and
  Setup Guide. The lower-frequency actions remain keyboard accessible and continue to route through
  typed webview messages or slash commands.
- optional native VS Code `@alysis` Chat Participant support using the stable VS Code 1.90 chat
  API. It contributes `/help`, `/plan`, `/execute`, and `/doctor` as entry points that open the
  Cockpit and delegate to existing extension controllers, while plain native chat opens the
  Cockpit rather than sending text directly to a model.
- protocol session creation, `chat.send`, `run.start`, structured assistant/tool/status/error rendering,
  host-managed chat approval UI, bounded event replay, honest cancellation, and backend-scoped
  artifact list/read. The typed client also exposes backend run/chat parity methods for
  `session.status`, `session.usage`, `session.history`, `session.context`, `session.compact`,
  `session.resume`, `session.modelInfo`, `session.subagents.status`,
  `session.subagents.setEnabled`, `session.images.list`, `session.images.add`,
  `session.images.clear`, `session.setMode`, `session.setModel`, `session.setStream`,
  `session.setActiveWorkdir`, and `session.clear`; the Cockpit can route to these methods without
  scraping terminal slash output.
  Bridge-client RPC methods are bound on the client instance, so typed controller paths can safely
  pass or store method references without losing request correlation or started-job tracking.
  `features.run_chat_options.session_method_capabilities` marks `session.history` as backend
  redacted/bounded, `session.context` token accounting as approximate `tokenizer_estimate` or
  explicitly unavailable, `session.compact` as live compactor-backed when available, and
  `session.resume` as bounded retained-session log replay, `session.modelInfo` as redacted
  provider/model metadata, and `session.subagents.*` as status/toggle-only live-session control.
  `/image` and `/paste-image` can accept a path or open a VS Code file picker; the backend still
  validates workspace scope, MIME/type, size, and symlinks.
  Chat only processes events for sessions/jobs it owns and does not attach to arbitrary bridge events
  when no chat session exists.
- grouped command-palette workflows for `Alysis Code: Session Actions`,
  `Alysis Code: Manage Profiles and Config`, `Alysis Code: Manage Tools and Skills`,
  `Alysis Code: Manage MCP, Hooks, and Conventions`,
  `Alysis Code: Manage Alysis Code Extensions`, `Alysis Code: Health and Diagnostics`, and
  `Alysis Code: Manage Forge Assets`. These are backed
  by a central backend action registry with required bridge methods/capabilities, mutation flags,
  Workspace Trust requirements, active session/plan requirements, and parameter collection strategy.
  The registry exposes safe management methods for `config.get`/`schema`/`validate`,
  guarded non-secret `config.set`, `profile.*`, retained `session.show`/`session.score`,
  `tools.catalog`, `tool.*`, `skill.*`, doctor summary/providers/bundle, sandbox doctor/setup/pull,
  cached-by-default `update.check`, `report.create`, MCP status/prompts/auth status/logout, hooks
  inspection/trust/toggle/init, conventions list/render, Alysis Code extension package
  search/list/info/install/remove/toggle, `forge.show`, `forge.attach`, and `forge.assets.*`.
  Mutating methods carry `workspace_trusted` params, are advertised with `mutates` and
  `trust_required` capability flags, require Workspace Trust on the extension side, and still fail
  closed in the backend if the extension forgets to gate them. Secret values are never accepted as
  protocol params or returned in config/profile payloads. Retained session output and rendered
  conventions are redacted and byte/event bounded before display. MCP OAuth on local extension
  hosts uses flow-id-bound start/status/cancel/logout, state + S256 PKCE, an owned loopback callback,
  bounded polling, encrypted token persistence, and shutdown fencing without returning an
  authorization code or raw token through JSONL. Every remote VS Code extension host remains explicitly unavailable until a
  remote-safe callback or device-code flow exists.
  `hooks.watch` and weak `hooks.watch.*` placeholders are not advertised until a subscription
  registry, bounded redacted event buffer, dropped-event accounting, watcher cancellation, and stop
  cleanup exist. `update.check` is not called from passive activation/status refresh; online checks
  require explicit user-triggered `allow_network` or `force` params.
  Backend action results render inside the cockpit, not only in the Output channel. However an
  action is launched (slash command, group Quick Pick, or hidden command), the cockpit Timeline
  shows a live result card that transitions busy → result: a spinner-by-glyph "Running…" state, then
  the structured payload as real DOM — a table for an array of objects (including a `{key:[…objects]}`
  wrapper such as `/tools` → `{tools:[…]}`, which is unwrapped and titled, scalar fields leading,
  capped at 50 rows / 8 columns), a key/value block for objects, or monospace text for raw output — or
  a redacted error message on failure. Nested values render as a compact `[N items]` / `{N fields}`
  summary rather than a stringified blob, a long scalar (>200 chars) truncates behind an inline
  expander, and a collapsed "Show raw" disclosure reveals the formatted (already-redacted) JSON on
  demand; the whole body is height-capped and scrolls inside its card. Honest non-error terminals
  (`{supported:false}` / `{available:false, reason}`, or `session.context source:"unavailable"`) render
  as a calm informational line, never an error or a misleading 0/empty table. A completed backend action
  shows a single result card: the info-severity `slash` lifecycle events ("started"/"routed") are not
  rendered as timeline cards and a completed action emits no redundant "X completed." notice — its
  running → ok card is the feedback. (A failure still surfaces through the standard error path — the
  error result card plus the error notice/Diagnostics — so errors are never hidden.) Payloads are
  redacted field-by-field before crossing to the webview (a structure-preserving deep redaction over
  the existing secret patterns), so secrets are scrubbed at every depth while the table/kv structure
  is preserved; the Output channel keeps the full detail as a secondary debug log. Backend actions
  have no cancellation path, so result cards are presented as non-cancellable (no cancel affordance).
  The result store + payload renderer are generic and reused by later management surfaces.
  Provider/profile switching and session management are first-class cockpit surfaces (not
  Quick-Pick-only). The cockpit header shows a click-to-switch provider/profile pill populated from
  `profile.list` (active profile marked); switching calls `profile.use` through the gated action
  pipeline, preserving the Workspace-Trust gate, the confirm modal, and the in-cockpit (redacted)
  result card, then reflects the new active profile in the header. The pill is gated on the
  `providersProfiles` capability and on Workspace Trust — when unsupported or untrusted it is
  informational/disabled with a reason rather than a faked control — and credentials still go
  through the existing Configure Provider / SecretStorage flow (never reimplemented). The Sessions
  view stores explicit row metadata for active, retained, closed, failed, and historical sessions.
  Show details, Score, and Usage pass the selected row's `session_id`; Score falls back to
  `latest: 1` only when no row is selected. Compact and Clear are visible only on the active live
  session row. Resume is visible only on retained/closed/failed/historical rows and replays that
  selected retained session into an existing live IDE session, or creates a new live IDE session
  first without sending a model prompt. Each route goes through the gated action pipeline so its
  result and errors render in the cockpit, redacted. Session compact/clear/resume require Workspace
  Trust in addition to their confirm modal; the empty Sessions state keeps the getting-started
  welcome.
  A first-class **Manage** tree under the Alysis Code activity-bar container browses Tools, Skills,
  MCP servers, Hooks, Conventions, and Extensions. Each domain group is gated on its capability
  (`tools`/`skills`/`mcp`/`hooks`/`conventions`/`ext`) — an unsupported domain renders the structured
  compatibility reason from bridge health, such as a newer-CLI version gate, security-model block,
  or IDE-not-applicable feature, rather than a faked control — and lists its items from the read-only
  `*.list`/`.catalog`/`.status` methods with their display text redacted. Native trees and the
  Cockpit read the same `CockpitRuntimeState` compatibility snapshot populated from successful
  bridge health, so a healthy CLI cannot leave the Manage tree stuck on empty compatibility.
  `Alysis Code: Manage Refresh` re-runs the bridge health probe before redrawing the tree; passive
  runtime updates only re-render the existing snapshot. Per-item context-menu
  actions (Show Details, Trust/Untrust, Enable/Disable, Remove/Uninstall, MCP Login/Logout) are
  exact-action-gated from `BackendActionMetadata`, the bridge's per-method capability flags, the
  selected item's state, and Workspace Trust. Unsupported actions are not shown as normal menu
  actions. MCP login appears only when the complete start/status/cancel lifecycle is advertised;
  after explicit confirmation VS Code opens the backend-provided HTTPS URL and shows cancellable
  progress until the encrypted credential write completes. Stateful actions only appear when meaningful: trust for untrusted tools,
  untrust for trusted tools, enable for disabled items, disable for enabled items, remove only for
  removable managed items, and MCP logout only for OAuth servers with a token. Show Details on a
  selected convention passes the row path as `focus_path` so it does not render all conventions by
  accident. The group Quick Picks stay as a fallback. The capabilities that previously had a bridge
  method but no extension caller are now wired as discrete, capability-gated actions with
  in-cockpit results where safe: `config.set` (guarded, non-secret) and `update.check`
  ("Alysis Code: Check for Updates" — user-initiated only, never auto-run, apply stays CLI-only). The
  sandbox/doctor/update diagnostics actions are grouped under the "Alysis Code: Health and
  Diagnostics" Quick Pick.
- The backend exposes capability-negotiated `mcp.server.status`, `mcp.server.enable`,
  `mcp.server.disable`, and `mcp.server.restart` for an exact live IDE session. It uses one stable
  connection lease per configured server across tool/resource/prompt bindings, requires Workspace
  Trust plus an idle session for mutations, fences reconnects against frozen catalogs, rejects
  list-change races, and withholds server-controlled diagnostics from JSONL errors. The extension
  has capability-negotiated typed client calls with strict redacted-response validation for this
  lifecycle. Its UI does not expose these controls yet; frontend wiring remains a separate follow-up.
- Forge Plan command and Activity Bar view backed by async `forge.plan.start`/`job.status`/
  `forge.plan.result` when available, synchronous `forge.plan` compatibility fallback,
  `forge.status`, structured Forge events, and backend artifacts. Tasks render with objective, file scope, acceptance
  criteria, verification commands, dependencies, risk notes, incomplete-plan warnings, source
  (`active_memory` vs loaded persisted plan), and status. `Alysis Code: List Forge Plans` and
  `Alysis Code: Open Forge Plan` can reopen persisted plans through `forge.list` and `forge.open`.
- Safe Forge backend parity methods are typed. `forge.attach` and Forge asset operations are
  reachable through `Alysis Code: Manage Forge Assets` and `/assets` / `/asset ...` slash routes:
  `forge.assets.list`, `forge.assets.show`, `forge.assets.add`, `forge.assets.delete`,
  `forge.assets.edit`, `forge.assets.refresh`, `forge.assets.cancelPending`,
  `forge.assets.checkPlan`, and `forge.assets.pruneLegacy`. `forge.show` is exposed through the
  grouped command and `/forge show` / `/plan show` slash routes; `forge.review` is exposed through
  the same grouped command and `/forge review` / `/review` slash routes. Mutating methods are
  Workspace Trust gated, source paths are workspace-scoped, symlinks and path escapes are rejected, and no
  terminal-output scraping is used. Compatibility prefers
  `features.forge.assets.supported` when present and falls back only to the real `forge.assets.*`
  method set for older CLIs.
- Safe Forge plan-edit parity is typed through `forge.plan.getState`,
  `forge.plan.setAssistant`, `forge.plan.setGoal`, `forge.plan.updateTask`, and
  `forge.plan.validate`, plus `forge.plan.regenerate` for Forge-context plan regeneration.
  `/assistant`, `/goal`, `/task`, `/forge plan state`, `/forge plan validate`,
  `/plan regenerate`, and `/forge plan regenerate` route to these methods. Global
  `/plan <instruction>` still creates a new Forge plan. `/assistant`, `/goal`, and `/task` are
  dual-mode read/update actions: show invocations are audited as read-only, update invocations are
  audited as mutating, and grouped QuickPick labels them as read/update. Mutating edits require
  Workspace Trust, persist plan state with optimistic revisions, and never parse terminal Forge chat
  output.
- `Alysis Code: Forge Execute Review`, backed first by `forge.executePreview`: it lets users select
  plan tasks, renders file scope, verification commands, sandbox/trust state, required approvals,
  blockers, and readiness. It starts the bridge through a no-secret path that strips both
  SecretStorage values and inherited `ALYSIS_API_KEY`, and preview uses no tool execution, no
  shell commands, and no file mutation.
- Forge Execute v1 review command path backed by `forge.execute`: after Preview reports readiness,
  the extension requires explicit user confirmation, restarts the bridge with credentials only when
  safe, reopens the persisted plan after a credential-bridge restart, refreshes backend-scoped
  artifacts/diffs, and starts a selected-task sequential review-mode job. The extension forwards
  `alysis.forgeExecuteMaxSteps` and `alysis.forgeExecuteNoLog` as explicit
  `max_steps`/`no_log` protocol params. The backend records terminal job status, completion
  time, and exit code before emitting the terminal `forge_execute_completed` or
  `forge_execute_failed` event that the UI replays. Execute is disabled for untrusted workspaces,
  unsupported modes, untrusted executable origins, missing strict sandbox readiness, or backend
  blockers.
- Forge session ownership is explicit. Forge owns the `session_id`/`job_id` returned by
  `forge.plan.start`, `forge.plan`, `forge.open`, and `forge.execute`, handles only those events,
  and ignores unknown/unowned events instead of consuming chat-owned traffic.
- Forge approval prompts are handled by the Forge controller, not the chat webview. The modal shows
  approval kind, reason, command/preview, files, and any backend-supported allow-for-session scope;
  the session-scoped option is shown only when the approval event marks it supported. A persistent
  cockpit approval card mirrors the prompt so users can answer from the Forge context without
  relying only on a modal. If the Forge controller is disposed or cannot answer, it denies
  fail-closed with a Forge-specific reason.
  `review`/`auto` modes are blocked in untrusted workspaces before bridge startup; readonly preview
  can run when executable-origin guards pass.
- `/plan` and `/forge plan` call the Forge controller directly with the typed instruction.
  `/execute preview` runs only the non-mutating preview path, while `/execute plan`, `/execute`,
  `/forge exec`, and `/forge execute` call the same Forge Execute Review flow as the command
  palette: task selection, Preview, blocker rendering, and explicit confirmation before any backend
  execution job starts. Broad execute arguments return an IDE v1 warning; auto/fullaccess execution
  remains blocked.
- Diff review foundation backed by `diff.list` and `diff.get`. Old/new text snapshots open through
  VS Code's native diff editor using readonly virtual documents; unified diff previews open
  read-only when side-by-side content is not available.
- Node unit tests, real Python stdio bridge smoke tests, and VS Code Extension Host integration
  tests through `@vscode/test-electron`
- SecretStorage-backed API keys are passed to the bridge through the child process environment,
  never command-line arguments, and only after the resolved executable origin is trusted
- SecretStorage helper for provider credentials
- Workspace Trust gates for Forge, process execution, workspace-scoped execution settings, and
  future mutating actions
- synchronized status bar, runtime chips, Timeline, Diagnostics panel, and Sessions view updates
- CLI recovery commands and cockpit setup cards for beta users with missing, old, broken, or
  incompatible CLIs. The extension copies/displays `pipx install alysis-code` and
  `pipx upgrade alysis-code`, but it never auto-installs or auto-upgrades the CLI.

The extension keeps the final Forge Execute flow disabled unless Preview reports
`real_execution_supported: true` and `preview_ready: true`. Review-mode execution shows real job
progress from structured events and `job.status`; unsupported modes remain fail-closed.

## IDE Bridge

The bridge should expose a stable local protocol instead of scraping terminal output. The initial
protocol should follow [IDE Protocol v1](ide_protocol.md) and cover:

- health and version metadata
- workspace attach/detach
- session start/resume
- user message submission
- safe run/chat options including model/base URL overrides, temperature, streaming, max steps,
  verification commands, no-log/yes flags, config-default subagent behavior, active workdir, and
  workspace-scoped image paths
- structured event stream
- host-managed approval requests and responses through `approval.respond`
- job/session cancellation
- job/session status, usage, history, context, compaction, resume, mode/model/stream changes,
  active workdir changes, clear, and bounded event replay
- Forge plan/status operations, selected-task review-mode Forge execute, cooperative Forge cancel,
  and future broader execution/swarm operations
- artifact, diff, and log lookup by opaque id
- structured config/profile/session/tool/skill/doctor/sandbox/update/report/MCP/hooks/conventions/
  extension-package management methods with per-method mutation/trust metadata and no inline secret
  params. Provider header values are routed to SecretStorage/configuration actions, and
  `ext.install` requires a reviewed package trust approval object in addition to Workspace Trust and
  confirmation. `skill.install` local sources are workspace-scoped directories or `.zip` archives;
  remote skill sources require explicit remote intent and confirmation. Extension package installs
  accept only registry ids or pinned HTTPS git sources and reject URL userinfo, local paths,
  traversal, unsupported schemes, and unpinned remote sources before install.

The bridge can build on server mode where the semantics already fit, but it should not force the
extension to depend on ad hoc HTTP endpoints that are missing product concepts. The current backend
has a first-class structured Forge planning protocol and opaque diff protocol foundation. The VS
Code extension now renders Forge plans and can open backend-provided diff snapshots/previews without
reading arbitrary workspace files.

## Event Model

The extension should consume structured events derived from the same surface contract used by the
CLI, including:

- status updates
- assistant message deltas and final messages
- tool start/output/end events
- patch and diff events
- approval requests
- errors and warnings
- Forge/swarm progress
- artifact creation and retention notices

Event payloads should be versioned, documented, and replayable enough for reconnect/resume. The
extension should not parse Rich console rendering or assume terminal formatting is protocol data.
The initial IDE bridge keeps only a bounded in-memory replay window; production extension behavior
should treat it as short-gap recovery, not durable history.

## VS Code Responsibilities

The extension should own VS Code-specific integration:

- Workspace Trust gating before agent startup or project-local capability loading
- SecretStorage for user-scoped tokens and bridge credentials
- command palette entries and status bar state
- webviews or tree views for chat, Forge plans, runs, artifacts, and logs
- native diff editors for patches and file changes
- approval UI for writes, shell commands, MCP/custom tools, and Forge execution
- timely `approval.respond` handling with a fail-closed timeout path
- independent chat and Forge session/job ownership so one controller cannot consume or deny another
  controller's events or approvals
- explicit rendering of approval result events, including timeout denial and scoped
  allow-for-session outcomes; "Allow for session" must only be offered when the backend event says
  `allow_for_session_supported: true` and provides a safe scope
- cancellation controls for active sessions and jobs
- diagnostics or output channels for bridge/runtime errors

The Managed Browser has two explicit actor/network scopes. Normal start sends
`network_scope: "public"`; this browser is shared with the top-level agent. **Start local testing**
is a direct IDE action only: after Workspace Trust and a fresh modal confirmation it sends
`network_scope: "public_loopback"` plus `confirm: true`. The backend isolates that session from
agent tools and allows public plus loopback destinations while continuing to deny LAN and
link-local. The extension never sends the legacy `allow_local_destinations` flag and never accepts
an executable override or profile path from either the model or webview. Start, navigate, click,
and type require Workspace Trust. Snapshot and diagnostic payloads remain backend bounded, while
screenshot bytes are read in bounded chunks, checked against declared size and SHA-256, validated
as PNG, and written only under the extension's private global-storage preview root. The webview
receives only a VS Code `asWebviewUri` URI; it never receives a host path. User export goes through
the native Save As dialog, and closing requires explicit confirmation before the exact owner-scoped
browser and its ephemeral screenshot are deleted. Exact close remains available after trust
revocation so cleanup cannot be stranded. Nested subagents do not receive browser tools.

Workspace Trust should fail closed. Untrusted workspaces may show read-only status or onboarding,
but should not silently enable project-local tools, hooks, plugins, MCP overrides, or write-capable
agent execution.

The current scaffold treats workspace-scoped execution settings as security-sensitive. In
Restricted Mode it ignores workspace or workspace-folder values for `alysis.cliPath`,
`alysis.defaultMode`, `alysis.defaultModel`, `alysis.baseUrl`, `alysis.provider`,
`alysis.transport`, `alysis.sandboxProfile`, `alysis.autoStartBridge`, and
`alysis.enableForge`. This protection is declared through VS Code `restrictedConfigurations` and
enforced in extension code before process execution.

Untrusted workspaces cannot cause the extension to execute a workspace-defined CLI path,
workspace-relative CLI path, absolute CLI path inside the open workspace, or default PATH-resolved
`alysis` binary inside the open workspace. SecretStorage API keys and inherited
`ALYSIS_API_KEY` values are not forwarded when the resolved executable path is not trusted. Users
must grant Workspace Trust before using workspace-local CLI wrappers or any write-capable flow.

The bridge client tracks the launch profile of the running stdio process: resolved executable path,
executable-origin trust, Workspace Trust, whether API-key forwarding was allowed, whether
`stripApiKey` was used, and whether `ALYSIS_API_KEY` was actually present in the child
environment. Safe readonly/no-secret operations such as `forge.list`, `forge.open`, and Forge
Execute Preview start with `stripApiKey` when no bridge is already running. Model-backed operations
such as Chat, Run Task, and Forge Plan require a credential-capable profile. If an idle no-secret
bridge is already running, the extension restarts it with credentials before the model-backed
operation; if pending protocol requests, active jobs, or pending approvals exist, the restart is
refused instead of silently reusing the weaker bridge.

Untrusted workspaces also block non-readonly mode before the bridge process is spawned. If the
effective default mode is `review`, `auto`, or any future write-capable mode, chat submission fails
before reading SecretStorage or constructing bridge environment variables. The user-facing recovery
is to switch `alysis.defaultMode` to `readonly` or grant Workspace Trust.

Allow-for-session approval is intentionally not broad by tool kind. Shell and custom/MCP-like
approvals require exact command scope; file operations require an exact file set or equivalent
backend-owned exact scope. Unsupported session approval requests are downgraded to allow-once by the
bridge and should be rendered as such by the extension.
The bridge advertises approvals with `round_trip: true`, `default_deny: true`,
`session_scoped_allow: true`, and a 300-second default timeout. Approval result events are
`prompt_for_input` payloads with `kind: "approval_result"` and only the resolved status/decision
fields needed to update pending cards.

The chat webview treats protocol payloads as untrusted text. Assistant output, tool output, file
paths, commands, warnings, and artifact content are assigned through `textContent`, not `innerHTML`.
The webview CSP uses `default-src 'none'`, data images only, nonce-gated script loading, stylesheet
loading from the extension webview source, and `base-uri`, `form-action`, and `frame-src` set to
`'none'`.

Slash command handling is an extension concern in v1. The router does not reuse terminal chat slash
parsing because terminal parsing is coupled to CLI state and rendering. Each supported slash command
maps to a typed extension controller or existing command-palette handler. `/cancel` routes through
the central cancel command, `/doctor` through the sandbox doctor command, and `/config` through the
provider configuration flow. `/config set` is limited to non-secret keys and still requires
Workspace Trust plus confirmation; provider credentials remain in SecretStorage. `/update` uses
cached/local status by default and performs a network check only for an explicit `online`/selected
flow. `/mode` updates the active IDE session through `session.setMode` when a session is live,
otherwise it mutates the VS Code `alysis.defaultMode` setting for future sessions. Only the
explicit `readonly`, `review`, and `auto` values are accepted; `review` and
`auto` modes remain blocked in untrusted workspaces.
Slash command lifecycle events are published into the Cockpit Timeline before the handler finishes.
For `/plan`, the extension records command start, route, workspace validation, executable-origin
checks, bridge start/reuse, planning start, and planning completion or failure. Early returns such
as disabled Forge, missing workspace, Workspace Trust failure, executable-origin guard failure,
bridge/protocol failure, and model/API failure are rendered as Timeline and Diagnostics warnings or
errors rather than only toasts. A single Forge Plan logical failure is represented by one canonical
runtime error card; duplicate runtime/Forge records with the same root cause are folded into that
card's Details disclosure and keep the existing **Open Output** action for full logs.

The cockpit runtime model tracks CLI Origin (`trusted`, `blocked`, `unknown`) separately from CLI
Health (`unknown`, `checking`, `ok`, `missing`, `incompatible`, `broken`). A trusted executable path
does not imply the binary supports `ide-bridge health` or `sandbox doctor`. Bridge process state
(`stopped`, `starting`, `ready`, `active`, `approval_needed`, `error`), bridge protocol health
(`unknown`, `ok`, `incompatible`, `missing_methods`, `error`), and sandbox doctor state
(`unknown`, `running`, `ok`, `failed`) share the same source of truth used by the status bar,
runtime chips, Diagnostics panel, and Timeline. The header renders only the high-level Mode, Model,
CLI Health, Bridge, and Sandbox summary; Diagnostics retains CLI Origin, protocol, provider,
Workspace Trust, and detailed sandbox/provider messages. `/doctor` records the safe command label
`alysis sandbox doctor --smoke`, exit code, and redacted stdout/stderr previews, with the full
output available from the Alysis Code Output panel instead of being dumped into the visible Timeline.

The extension/CLI compatibility contract is feature-scoped:

- Baseline Cockpit requires `initialize`, `health`, and `getCapabilities`.
- Chat/run requires `session.create`, `chat.send`, `run.start`, `session.cancel`, `approval.respond`,
  `job.status`, `session.list`, `session.getEvents`, `artifact.list`, and `artifact.read`.
- Run/chat backend parity requires `session.status`, `session.usage`, `session.history`,
  `session.context`, `session.compact`, `session.resume`, `session.images.list`,
  `session.modelInfo`, `session.subagents.status`, `session.subagents.setEnabled`,
  `session.images.add`, `session.images.clear`, `session.setMode`, `session.setModel`,
  `session.setStream`, `session.setActiveWorkdir`, and `session.clear`.
- Management backend parity requires `config.get`, `config.set`, `config.schema`,
  `config.validate`, `profile.list`, `profile.show`, `profile.add`, `profile.remove`,
  `profile.use`, `profile.rename`, `profile.presets`, `profile.preset`, `profile.convert`,
  `session.show`, `session.score`, `tools.catalog`, `tool.list`, `tool.info`, `tool.trust`,
  `tool.untrust`, `skill.list`, `skill.info`, `skill.init`, `skill.validate`, `skill.install`,
  `skill.enable`, `skill.disable`, `skill.remove`, `doctor.summary`, `doctor.providers`,
  `doctor.bundle`, `sandbox.doctor`, `sandbox.setup`, `sandbox.pull`, `update.check`,
  `report.create`, `mcp.status`, `mcp.prompts.list`, `mcp.prompts.get`, `mcp.auth.status`,
  `mcp.auth.login.start`, `mcp.auth.login.status`, `mcp.auth.login.cancel`, `mcp.auth.logout`, `hooks.list`, `hooks.doctor`, `hooks.trace`,
  `hooks.test`, `hooks.trust`, `hooks.untrust`, `hooks.init`, `hooks.effective`, `hooks.enable`,
  `hooks.disable`, `conventions.list`, `conventions.render`, `ext.search`, `ext.list`,
  `ext.info`, `ext.install`, `ext.uninstall`, `ext.enable`, and `ext.disable`.
  `ext.install` is a two-step backend flow: first fetch structured manifest/source/permission
  trust metadata, then call again with the unchanged `trust_approval` payload after explicit user
  review. `yes: true` alone is not package trust.
  `update.check` defaults to cached/local status and reports whether a network check was used.
  `session.show`, `session.history`, `session.resume`, and `conventions.render` return
  bounded/redacted payload metadata so output panels do not display raw retained logs, replay
  history, or convention text without caps.
- Forge Plan requires `forge.plan` or `forge.plan.start` plus `forge.plan.result`.
- Persisted Forge plans require `forge.list`, `forge.open`, `forge.status`, and can use
  `forge.show` for richer backend detail.
- Forge plan edits require `forge.plan.getState`, `forge.plan.setAssistant`,
  `forge.plan.setGoal`, `forge.plan.updateTask`, `forge.plan.validate`, and
  `forge.plan.regenerate`. The extension action metadata marks `/assistant`, `/goal`, and `/task`
  as parameter-determined read/update routes so Cockpit action results and release checks cannot
  treat mutating updates as read-only.
- Forge asset command/slash workflows use `forge.attach`, `forge.assets.list`,
  `forge.assets.show`, `forge.assets.add`, `forge.assets.delete`, `forge.assets.edit`,
  `forge.assets.refresh`, `forge.assets.cancelPending`, `forge.assets.checkPlan`, and
  `forge.assets.pruneLegacy`. `forge.review` is routed through the grouped Forge Assets command
  and `/forge review` / `/review` slash actions. The old `assets.list` method name is not a Forge
  assets compatibility signal.
- Forge Execute Preview requires `forge.executePreview`.
- Forge Execute Review requires `forge.execute`, host approvals, job/event replay, artifacts, and
  diff methods.

When `alysis ide-bridge health` is missing, the CLI is treated as incompatible and the extension
does not start the bridge or read/forward SecretStorage API keys. When `sandbox doctor` is missing
or fails, the Sandbox chip and Diagnostics panel show an unavailable/failed doctor state without
turning executable-origin trust into a green health signal. Missing feature methods disable only
the affected command/UI path; the Cockpit groups all such gaps into one root-cause recovery card in
Diagnostics (with a single low-noise Timeline pointer) rather than stacking one card per feature.

Recovery distinguishes *unreachable* from *outdated*. A CLI that cannot be found or launched (not on
`PATH`, a wrong `alysis.cliPath`, or a failed bridge transport) shows one **"Can't reach the
Alysis Code CLI"** card whose primary action is **Locate Alysis Code CLI** — locating the right binary,
not upgrading, is the fix. A CLI that responds but is too old (wrong protocol or missing the
ide-bridge surface) shows one **outdated** card whose primary action is **Upgrade**. A single
detection failure renders exactly one card: the bridge-process error it implies is folded into that
card instead of spawning a second "bridge-unreachable" sibling, and no standalone "Bridge health
failed" item is recorded beside it (the card's **Open Output** action still surfaces full logs).
When the bridge is healthy, Diagnostics shows a calm affirmative line (`● Bridge healthy — <mode> ·
<model>`) instead of bare status frames. On a recoverable failure the extension also re-checks health
automatically with bounded backoff (a few attempts with increasing delay), clearing the cards and
flipping back to ready on its own — each attempt re-checks the Workspace-Trust / executable-origin
guard and never auto-spawns a blocked CLI. A manual **Reconnect**/**Restart Bridge** remains always
available, and it re-reads `alysis.cliPath` and re-runs discovery, so a path set via Locate CLI
applies live without a window reload.

Native VS Code `@alysis` Chat Participant support is enabled as an optional thin router on top
of the Cockpit. The extension uses the stable VS Code 1.90 `vscode.chat.createChatParticipant` API
and contributes `/help`, `/plan`, `/execute`, and `/doctor` commands without proposed APIs or a
GitHub Copilot dependency. The Cockpit remains the primary Alysis Code UI: native chat opens the
Cockpit, routes supported commands through the same `SlashCommandRouter` and controllers, fails
unknown native slash commands closed, and does not duplicate Forge state inside the native chat
view. Normal `@alysis` text opens the Cockpit rather than silently sending prompt text to a
model. The participant obeys Workspace Trust, executable-origin checks, CLI compatibility,
SecretStorage forwarding guards, Preview blockers, and approval boundaries.

The cockpit intentionally labels execution as Forge Execute Review. It must not describe the IDE
path as full Forge Swarm, auto execution, fullaccess execution, merge orchestration, or active
runtime cancellation unless those protocol operations are actually wired and tested. The Execute
panel should keep disabled actions visible with the backend-provided blocker reason rather than
implying hidden support.

Forge protocol support is split deliberately. `forge.plan` creates or reuses a bridge session,
records a normal Forge run under `.alysis/runs/<plan_id>/`, calls the Forge planner path instead
of producing a shallow one-task scaffold, returns a stable task schema, exposes plan JSON/Markdown
as artifacts, and emits structured status/plan/warning events. Plans that cannot be produced because
the planner/model/API key is unavailable fail closed with a protocol error; incomplete plans are
flagged with warnings instead of silently pretending to be execution-ready. The Forge Plan command
prompts for an instruction, validates Workspace Trust and executable origin before bridge spawn,
uses `forge.plan.start`/`job.status`/`forge.plan.result` when the live bridge advertises async
planning support, keeping those calls receiver-qualified so clicking Plan starts and tracks the
backend job instead of detaching bridge methods. It falls back to synchronous `forge.plan` only for
older compatible bridges. The
Forge Plan tree shows active planning status from structured events and never parses terminal
output. `forge.list` and
`forge.open` let the extension reopen persisted plans after session close or bridge restart through
opaque plan ids scoped to the workspace registry. Persisted Forge plan and artifact roots are
validated by the backend; symlinked `.alysis`, run, `plan`, `execution`, and `execution/patches`
components are rejected instead of followed. `forge.executePreview` and `forge.execute` with
`dry_run: true` provide a non-mutating readiness summary for selected tasks. Real IDE
`forge.execute` v1 is review-mode-only, requires explicit selected tasks, Workspace Trust, strict
sandbox readiness, plan completeness, and scoped host-managed approvals, and starts a sequential
backend job that emits structured
verification/review events plus scoped artifacts/diffs. Terminal Execute completion/failure events
are emitted only after `job.status`, `completed_at`, and `exit_code` are terminal, so Sessions view
refreshes should not show a stale running active job after terminal Execute events. The preview also
renders dynamic runtime approval requirements so users know shell commands require exact command-hash
approvals and custom/MCP-like tool requests cannot receive broad session approval by tool kind.
Extension callers pass configured `alysis.forgeExecuteMaxSteps` and
`alysis.forgeExecuteNoLog` explicitly; omitted `max_steps` means the CLI config default is used.
Failed verification and blocked review decisions are rendered as failure states, and dependent
selected tasks do not start after a prerequisite task fails. `auto`/`fullaccess` Forge Execute
remains fail-closed. The separate Run Swarm console supports review-only isolated parallel work,
durable recovery, task-attributed approvals, and per-task Keep/Discard; it never auto-merges or
writes the base worktree. `forge.cancel` is
presented only when `features.forge.cancel.supported` is true and uses cooperative checkpoint
cancellation, not a hard interrupt. The shared `Alysis Code: Cancel Current Run` command routes to
Forge when Forge owns the active job, shows `cancellation_requested` as an in-progress state, and
does not report terminal cancellation until the backend reconciles the job. The IDE v1 path does not
claim the full CLI `forge swarm` contract: integration-gate batch merge semantics and full
merge/push orchestration remain out of the IDE surface.

Diff protocol support is opaque-id based. `diff.list` returns only diffs known from recorded Forge
run artifacts for the requested session and optional plan, and `diff.get` requires `session_id` and
`plan_id` so it resolves only those ids with byte limits and protocol preview redaction. The
extension opens native side-by-side diffs only from backend-provided old/new text or explicit
artifact ids; otherwise it opens the backend-provided unified diff preview read-only. It must not be
treated as a generic workspace file-read API. Symlinked patch directories and diff files are not
treated as valid diff sources.

## Alysis Code Responsibilities

The Python package should own:

- workspace binding and trust-sensitive runtime policy
- mode semantics (`readonly`, `review`, `auto`, `fullaccess`)
- sandboxing and command safety checks
- tool registration and validation
- session logging and artifacts
- Forge plan validation, execution, swarm orchestration, and cancellation
- model/provider configuration and API-key resolution

The extension should call these capabilities through the bridge rather than reimplementing them.

## Production Readiness

Production-ready VS Code support requires:

- protocol compatibility tests between extension and bridge
- mock-bridge extension tests for UI state, approvals, cancellation, and reconnect behavior
- integration smoke tests against a real local Alysis Code bridge
- clear behavior when the installed Alysis Code version is missing, too old, or incompatible
- documented storage boundaries for SecretStorage, workspace files, session logs, and artifacts
- no claims in README or release notes that the scaffold is a published or feature-complete extension

Extension deactivate is awaitable. Chat and Forge controllers deny only their own pending
approvals, ask the bridge to close idle sessions or request cooperative cancellation for advertised
active jobs, and then let the shared bridge client stop the stdio process with a bounded graceful
attempt before SIGKILL is used as the last resort. Jobs that do not reach terminal backend status
during shutdown are treated as interrupted after recovery, not as successfully cancelled.

## Test And Release Infrastructure

The extension has three local verification layers:

- `npm test` for TypeScript unit tests plus a real Python bridge smoke. The smoke starts
  `python -m alysis_code.cli ide-bridge --stdio` from the source checkout and exercises
  `initialize`, `getCapabilities`, safe `session.create`, `session.list`, and `session.cancel`
  without sending chat or contacting a model provider.
- `npm run test:integration` for VS Code Extension Host tests using `@vscode/test-electron`. These
  tests run with an external mock CLI fixture outside the packaged extension so they can activate
  the extension, verify commands/views/status, exercise SecretStorage, open the cockpit webview,
  run slash-command Forge Plan and Execute Preview flows, run a deterministic review-mode
  `/execute plan` mock with verification/review events, verify approval cards, open mock diffs and
  artifacts, report `/doctor` failures in Timeline and Diagnostics, exercise broken health and
  missing-method status chips, reject unknown slash commands locally, and check Workspace Trust gates
  without contacting providers. CI pins `VSCODE_TEST_VERSION=1.90.0`; the runner verifies a provided
  `VSCODE_TEST_EXECUTABLE_PATH` before launch, or preflights the VS Code download host and fails with
  a clean cached-executable/download-unavailable message before `@vscode/test-electron` can emit a
  raw DNS stack trace. Environments that cannot download VS Code should set
  `VSCODE_TEST_EXECUTABLE_PATH` to a pre-downloaded executable. Release workflows must run the
  Extension Host tests rather than silently skipping them.
- `npm run package` for strict local VSIX creation through `npm run package:vsix`.
- `npm run package:pre-release` for a Marketplace pre-release VSIX candidate without publishing.
- `npm run prepublish:check` for compile, lint, tests, high-severity audit, and strict packaging
  before any Marketplace upload is considered. `npm run lint` type-checks the TypeScript sources
  (`tsc --noEmit`), the webview JS (`tsc -p tsconfig.webview.json` — `media/*.js` carry `// @ts-check`
  and are checked against `media/webview-globals.d.ts`), and runs ESLint over both `src/` and the
  now-included `media/**` via the flat `eslint.config.js`.
- `npm audit --audit-level=high` for extension dependency hygiene. Current high-severity findings
  are removed with package overrides for Mocha's dev-only transitive dependencies; those packages are
  excluded from the VSIX.
- `python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host skip` for deterministic
  local smoke without network. This path is useful before packaging, but the report is explicitly
  marked component-only and not release-valid.
- `bash scripts/qa/check_vscode_extension_release_candidate.sh` for the local generic component gate.
  It runs parity, targeted protocol tests, extension npm checks, packaging, and then dogfood.
  `ALYSIS_RELEASE_DOGFOOD=mock` is the default and runs the skip-host smoke; use
  `ALYSIS_RELEASE_DOGFOOD=extension-host` or `require-cached` for full component dogfood, and
  `ALYSIS_RELEASE_DOGFOOD=skip` only for explicit local iteration. For release signoff, set
  `ALYSIS_RELEASE_STRICT=1`; strict mode rejects skipped/no-host dogfood, including the default
  local mock skip-host smoke, verifies the pre-release VSIX marker, rejects a managed manifest,
  and validates `status: passed`,
  `component_valid: true`, `release_valid: false`, and `extension_host: passed`.
- `python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host run` for component
  dogfood when VS Code download is allowed, or
  `VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host require-cached`
  for cached/offline release dogfood. The harness copies a no-dependency disposable Python fixture,
  initializes git, verifies the fixture, checks `alysis ide-bridge health` with the active Python
  interpreter plus `PYTHONPATH=src`, verifies or packages the VSIX, runs the Extension Host mock
  bridge Cockpit scenario, and writes evidence under `qa_reports/vscode_dogfood/` without requiring
  model credentials. Generic reports record `component_valid: true/false`,
  `release_valid: false`,
  `extension_host: passed/skipped/failed`, and an `extension_host_preflight` object with the cached
  executable or download preflight result, so local smoke evidence cannot be mistaken for a
  production gate. The script does not default to `uv run`; pass `--cli-command` only to test an
  installed binary intentionally.

CI includes a `vscode-extension` job that checks out the repo, sets up Python and Node, runs
`npm ci`, compiles, lints, runs unit/bridge smoke tests, packages the VSIX, and runs Extension Host
tests under `xvfb-run` on Linux. The component-candidate workflow runs component dogfood with the
generic packaged VSIX and `--extension-host run` or `require-cached`, then uploads explicitly
not-for-release artifacts. Environments that cannot download VS Code should set
`VSCODE_TEST_EXECUTABLE_PATH` and use `--extension-host require-cached`. Dogfood reports under
`qa_reports/vscode_dogfood/` are uploaded on CI failure. The workflow uploads the VSIX only from
default-branch pushes or manual `workflow_dispatch`; it does not publish automatically.

The manual `.github/workflows/vscode-extension-release-candidate.yml` workflow is the generic
component gate. It runs the explicit Python, Windows/default-console encoding, and extension checks,
including `python scripts/qa/check_ide_cli_parity.py` and the parity/protocol contract pytest files,
plus the IDE protocol and stdio bridge tests, packages a pre-release VSIX and verifies the manifest
pre-release marker, requires Extension Host component dogfood, uploads only not-for-release VSIX and
dogfood reports, and never runs `vsce publish`.
The dispatch form requires either an existing checked-out manual real-provider dogfood report path
inside the workspace, confirmation that no P0/P1 blockers are open, and confirmation that known
limitations are current. HTTPS evidence URLs are rejected for deterministic CI evidence.

Release-candidate signoff requires the full Python suite to be green, including
`tests/test_ide_cli_parity_matrix.py`, `tests/test_ide_protocol_contract.py`,
`tests/test_surface_console_encoding.py`, `tests/test_forge_exec.py`, `tests/test_forge_swarm.py`,
and `tests/test_forge_review.py`, plus the VS Code `npm ci`, compile, lint, test, high-severity
audit, package, and Extension Host integration steps, plus component dogfood with Extension Host
enabled. Production signoff additionally requires the six-target managed-runtime workflow and the
exact target's isolated installed-production summary. Before evidence attestation it also requires
the protected six-target installed live-provider workflow, the installed Linux untrusted-workspace
workflow, and the dedicated real WSL and Remote-SSH acceptance workflow against that same immutable
candidate. It also requires manual VSIX dogfood using
`python scripts/qa/vscode_extension_dogfood.py --mode manual-real-provider --vsix-path
/absolute/path/to/vscode-alysis-<target>.vsix --package-mode require` to create the disposable
repo and checklist for the Cockpit `/doctor` -> `/plan` -> `/execute preview` -> `/execute plan` ->
approval -> diff/artifact workflow. Manual signoff must include Windows default console smoke,
macOS smoke, Linux smoke, broken-CLI recovery, missing-provider recovery / missing-API-key recovery, and no-secret screenshot
or video capture when release notes need media. Every OS smoke record must cover CLI discovery,
bridge startup, SecretStorage, chat/run, image path handling, Forge plan/execute, artifacts/diffs,
extension reload, and Workspace Trust behavior. A real Forge Execute v1 review-mode smoke in a
disposable repository is required before public release claims are made.

## Beta Release Boundary

The extension has three release channels:

- Internal VSIX dogfood for maintainer-only local package installs.
- Public beta / Marketplace pre-release for users who explicitly opt into the pre-release channel.
- Stable Marketplace release only after beta evidence and support readiness.

The first public-beta candidate line is `vscode-alysis` `0.1.x`. VS Code Marketplace versions
remain `major.minor.patch`; pre-release status is controlled by `vsce --pre-release`, not by
putting `0.1.1-beta.1` in `package.json`. Release candidate tags should be named like
`vscode-alysis-v0.1.1-rc.1`, public beta tags like `vscode-alysis-v0.1.1-beta.1`, and the
first stable Marketplace line like `vscode-alysis-v0.2.0` or later.
Do not publish `0.1.1` to the stable Marketplace channel while this remains a beta candidate.

Known IDE v1 limitations must remain explicit in release notes and Marketplace copy: Forge swarm is review-only (per-task Keep/Discard, never auto-merged) and capability-gated; broad Forge exec and auto/fullaccess execution unavailable; cancellation is
cooperative checkpoint only with no hard interrupt; MCP OAuth is unavailable in Remote-SSH
extension hosts until a URI-handler or device-code callback exists; agent-controlled Managed Browser access is public-only and unavailable to nested
subagents, while direct IDE local testing is loopback-only;
`hooks.watch` unavailable; raw trace unavailable; arbitrary terminal start and interactive terminal
streaming unavailable; setup/config terminal menus and inline API key setters unavailable.

Marketplace readiness requires the package manifest metadata to remain accurate: repository,
publisher, PNG icon, categories, keywords, and gallery banner should be reviewed before release.
Publishing also requires Marketplace publisher access and a scoped Azure DevOps Marketplace PAT
stored as `VSCE_PAT` only in the protected `vscode-marketplace-beta` environment. Maintainers should
run `npm run package:pre-release`, install the generated VSIX manually, and complete the schema-v5
provider plus schema-v3 environment evidence and the installed-provider, actual-untrusted, and
real-remote workflows. The protected evidence workflow must attest those exact candidate-bound
reports before promotion is dispatched with every retained run identity and the reviewed inventory
digest. Direct local `vsce publish` and manual upload are not release paths: protected promotion
reconciles existing target bytes, publishes only missing pre-release targets, and verifies the
observed six-package Marketplace state. If a beta package must be pulled, use the Marketplace
publisher portal to unpublish the affected version, publish a fixed pre-release where possible, and
document the rollback in release notes.

Known beta limitations:

- IDE Forge Execute v1 is selected-task review-mode execution.
- `no_log` and `max_steps` are explicit protocol params. The extension sources them from
  `alysis.forgeExecuteNoLog` and positive `alysis.forgeExecuteMaxSteps`; `max_steps` omitted
  means use the CLI config default.
- Subagents are advertised as unsupported for IDE Forge Execute v1. The bridge rejects
  `subagents_enabled` params instead of silently ignoring them until worker lifecycle, sandbox,
  approval, and cancellation guarantees are strong enough.
- IDE auto/fullaccess execution is unsupported.
- Active runtime cancellation uses cooperative checkpoints; hard interrupt is unavailable.
- Full CLI-style parallel swarm, integration-gate batch merge, replanning, and merge/push
  orchestration remain outside the IDE path until explicitly wired and tested.
- The extension requires a local Alysis Code CLI with IDE Protocol v1.
- VS Code Web is unsupported because the extension launches a local CLI process.
- Agent-controlled Managed Browser is public-web-only, and nested-subagent browser tools remain
  unavailable. Direct IDE local testing is explicitly confirmed and loopback-only; LAN and
  link-local remain denied. User-saved screenshots leave ephemeral storage only through native
  Save As.
- Extension Host tests require either a real VS Code binary or network access for
  `@vscode/test-electron`.

The extension support docs cover the expected failure modes: missing CLI, incompatible bridge
protocol, untrusted workspace behavior, ignored workspace-scoped settings, SecretStorage API keys,
sandbox doctor failures, WSL/path mismatches, and cooperative cancellation waiting for backend
checkpoints.
