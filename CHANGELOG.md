# Changelog

All notable changes to Alysis Code will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **Renamed from Sylliptor to Alysis Code.** The command is now `alysis`, the
  PyPI package is `alysis-code`, the Python module is `alysis_code`, the
  environment prefix is `ALYSIS_`, and the per-repo directory is `.alysis/`.
  See [docs/migration-alysis-code.md](docs/migration-alysis-code.md).

  Existing installs keep working. Config and credential directories are copied
  to their new locations on first run; the MCP OAuth keyring entry and any
  token blobs sealed under the previous authenticated-data tag are adopted
  automatically; a Pro profile named `sylliptor` is renamed along with its
  stored gateway key; every `ALYSIS_*` variable falls back to its `SYLLIPTOR_*`
  predecessor with a one-time deprecation notice; hooks and custom tools
  receive both spellings; `.sylliptor/` directories already committed to a repo
  are used in place rather than renamed; both plugin manifest filenames load
  and `compatibility.sylliptor` remains a valid spelling of
  `compatibility.alysis`; and `sylliptor` stays installed as a deprecated
  command alias.

  Two things need doing by hand: reinstall the VS Code extension (Marketplace
  extension IDs are immutable, so the renamed extension is a new listing —
  settings migrate on first activation, installs and reviews do not), and
  update any pinned CI or Docker references to `sylliptor-agent-cli` or
  `ghcr.io/alysisai/sylliptor-sandbox`.

### Added

- **Runaway subagent recovery.** Parent waits can wake for steering or repeated child work, persistent identical outcomes end as sanitized incomplete runs, and live activity replaces canned role narration.
- **Subagent TUI cleanup.** Added `/subagents`, safer compact panel summaries, one footer count badge, and retired the singular command and `test-strategist` role.
- **Live subagent pane in the terminal TUI.** A bounded pane above the input
  follows one child run at a time without obscuring the streaming transcript.
  Ctrl+N and Ctrl+B rotate through the main agent and children in spawn order,
  while Esc returns to the main-agent view. Incremental event reads keep polling
  proportional to new activity and the pane remains available at every trace level.
- **Visible partitioned subagent batching.** Parallel-safe calls now run concurrently while unsafe
  siblings remain sequential, with an audit notice and a default-off opt-in for shared non-writing
  shell roles.
- **Deferred configuration controls during terminal turns.** `/mode`, `/persona`,
  `/model`, and typed `/config` commands can be staged while an agent is working
  and apply before the next queued turn without altering the active one. Bare
  `/config` still opens immediately; saves reach disk at once
  and reload the live session after the turn. Cross-thread environment-context
  rewrites are now refused and recorded instead of racing the active worker.
- **Mid-turn input in the terminal TUI.** Enter sends a durable user message to a
  running agent at its next safe step boundary, while Ctrl+Q queues a separate
  follow-up turn. Read-only slash commands remain available during execution,
  pending input is bounded and visible, and interrupting a turn discards its
  queued work.
- **Subagent orchestration workstream.** The completed stack now includes:
  - **Roster:** an evidence-based `verifier`, manual routing visibility for planning-only roles,
    custom role visibility, and exact post-filter child tool catalogs.
  - **Orchestration:** synchronous and background children, bounded parallel shared-readonly and
    isolated work, retained worktree candidates with explicit apply/discard, pinned verification,
    bounded writer helpers, parent steering, dependency chains, batch telemetry, persisted view,
    and terminal-run resumption. Parent deadlines, cancellation, usage replay, and turn-end joining
    cover queued, waiting, and running states without extending Forge or swarm surfaces.
  - **Scout:** capability-gated `dependency-scout` research pins local dependency versions and
    requires successful web evidence before reporting success.
  - **Fixes:** directly constructed sessions retain same-response parallel batching, child approval
    prompts are serialized, lifecycle state is visible in the TUI, malformed tool-call modes fail
    explicitly, empty isolated candidates release immediately, and session artifacts honor the
    external data root. Terminal events preserve the full report beside retained-worktree notices,
    empty blast-radius attribution renders cleanly, and incomplete children expose their exact stop
    reason, budget facts, retained run ID, and resume command. Synchronous and background children
    now share that run-ID namespace; incomplete applies require explicit acknowledgement; deadline
    admission uses a recent-call median instead of one latency outlier; execution roles fail fast
    when their required tool is unavailable; and TUI child timers use the child's own start time.
    Resumed children inherit still-valid read-ledger evidence, nested helpers must fit two robustly
    estimated calls before launch, deadline summaries agree with blocking events, cancellation
    results distinguish transitioned/already-finished/unknown runs, and blast-radius reporting
    trusts authoritative structured verification even when raw output is truncated.
  - **Evaluation hardening:** reviewers and verifiers inspect the current diff before surrounding
    source, explorer maps replace parent re-discovery, truncated reads name their exact continuation,
    repository-declared checks are authorized safely, and capability-gated verifiers can collect
    managed-browser `Visual QA` evidence without treating browser artifacts as workspace edits. The
    code-reviewer now mechanically lacks whole-file and history reads; review, fix, and verification
    are explicit decision points; Python-native pytest, unittest, Ruff, and mypy checks join safe
    discovery. Delegation guidance accounts for the fresh context/cache cost of implementation
    children and reserves them for parallel, verify-before-apply, or risk-isolation value. The
    model-facing additions remain inside the startup prompt budgets.
- **Measured context-efficiency controls.** Parent and child sessions independently suppress
  unchanged overlapping `fs_read`/`fs_read_lines` output while retaining a `force=true` escape
  hatch and hash-based invalidation after file changes. A default-off cache keepalive can replay a
  parent's exact request prefix with `max_tokens=16` during long synchronous-child waits; its calls,
  tokens, and provider operation are separately visible, and deadline finalization always wins.
  It now refuses response-continuation and subscription/gateway transports after a live rerun showed
  four pings spending about 213k uncached tokens to protect a roughly 49k-token cold wake.
- **GLM 5.3 through the Z.AI Coding Plan.** A separate subscription-backed preset now
  targets Z.AI's dedicated OpenAI-compatible Coding Plan endpoint without changing the
  China/general Zhipu pay-per-token profile. It offers the plan's current GLM-5.3,
  GLM-5-Turbo, and GLM-4.7 roster, records GLM-5.3's 1M context and 128K output limits,
  treats usage as plan credits rather than invented token prices, and exposes only the
  documented low/high/max reasoning floor—there is no misleading reasoning-off option.
- **NVIDIA NIM hosted provider and live model selection.** Setup now authenticates once with
  `NVIDIA_API_KEY`, merges NVIDIA's live `/v1/models` inventory with a small offline-safe
  recommendation set, and lets users choose NVIDIA- and third-party-hosted models such as
  DeepSeek without changing provider routes. Manual model entry stays near the top of the
  picker. Reasoning controls are allowlisted per model on NVIDIA's hosted surface: Nemotron
  Super, Ultra, and Nano and DeepSeek V4 expose only their documented choices, while unknown
  models use the provider default without guessed fields. Model-list parsing trusts explicit
  capability metadata and selected-model validation rather than English fragments in model
  IDs. NVIDIA Free Endpoints remain rate-limited prototyping endpoints rather than a
  production SLA.
- **Qwen3.8-Max and the current DeepSeek V4 catalog.** Every QwenCloud regional preset now
  offers `qwen3.8-max` as its advanced multimodal flagship while retaining `qwen3.7-plus` as
  the balanced default; OpenRouter offers the same model through its provider-qualified id.
  DeepSeek's first-party and OpenRouter presets add the experimental vision-capable Flash route,
  while OpenRouter, Together, and Fireworks use current dated Pro 0813 and Flash 0731 ids.
  Official context, output, capability, cache, and token-price metadata fill gaps in the bundled
  catalog; first-party DeepSeek estimates use conservative peak rates because its discount is
  time-dependent, and QwenCloud costs stay unknown because they vary by region and currency.
  Qwen, DeepSeek, Together, and Fireworks now emit only the reasoning controls documented for
  each surface, including Together's distinct disable object and opaque tool-call state replay.
- **DeepSeek thinking continuity.** First-party and hosted DeepSeek routes now retain and replay
  opaque reasoning state after every tools-enabled assistant turn, including ordinary replies.
  Omitted thinking configuration follows DeepSeek's documented default-on contract, and
  incompatible `tool_choice` controls are suppressed while reasoning is active. The first-party
  effort allowlist now includes DeepSeek's distinct `low`, `high`, and `max` values. A macOS
  launcher starts the current checkout using the active configured provider without handling or
  overriding API credentials.
- **Grok 4.6 and current Gemini production models.** The xAI preset now defaults to Grok 4.6
  with its official 500K context, vision/reasoning capabilities, base pricing, and `xhigh`
  reasoning contract; Grok 4.5 remains available as a fallback. The native, compatibility, and
  native-alias presets now offer Gemini 3.7 Flash, Gemini 3.6 Flash, and Gemini 3.5 Flash-Lite,
  backed by official context, output, capability, and pricing metadata while the bundled LiteLLM
  snapshot catches up. Gemini 3.7's reasoning contract rejects the unsupported `minimal` effort.
- **`report_blocker` introduces a structured blocker channel for completion-gated turns.**
  Top-level execute runtimes can return a free-form, provider-neutral user explanation through
  a built-in tool; the host trusts the successful tool result rather than words, language, or
  punctuation in the message. Accepted reports reuse the existing completion-state gate, emit
  `blocker_reported`, terminate with reason `blocked`, and become the final response without a
  second model call. The tool is hidden from subagents and unrelated runtimes.
- **Per-attempt LLM timeout ceiling (endpoint fast-fail).** A single LLM request under an
  active run deadline is now capped at 900s by default (`SYLLIPTOR_LLM_ATTEMPT_TIMEOUT_S`;
  off-words disable), so one hung provider read becomes an ordinary retryable timeout for the
  provider-retry ladder instead of consuming the rest of the wall clock — observed in a scored
  run where a read blocked ~60 minutes until the budget died. Outside the finalization window
  the deadline clamp now also protects the finalization reserve, so a slow request times out
  with enough budget left for the wrap-up call (inside the window, behavior is unchanged).
  The provider-retry gate is deadline-fitted: a retry is only allowed when the backoff sleep
  plus a minimally useful attempt still fits the remaining budget, and the next attempt's
  timeout is shrunk to that budget so no retry can outlive the deadline.
- **Provider-failure salvage in one-shot runs.** When the endpoint dies after retries
  (`provider_unavailable`/`provider_throttled`/`infra_unavailable`) mid-turn, a one-shot run
  with material work persisted now stops locally with a runtime summary and exit 0
  (`provider_failure_salvage` event) instead of discarding the work behind exit 75. Salvage
  must be earned by the turn's own material edits — a pre-existing workspace diff in a dirty
  repository is never counted as evidence. Runs that produced nothing keep the loud
  infrastructure exit so operators and retry machinery still see it; interactive turns still
  surface the error unchanged.
- **Vendored-tree edit advisory.** `externals`, `_vendor`, `third_party`, and `thirdparty`
  now classify as generated/vendored (joining `vendor`, `node_modules`, ...), and the first
  material edit landing in such a tree emits a one-shot advisory naming the paths
  (`vendored_path_edit_advisory`) — advisory only, never blocks. Motivated by a scored run
  that rewrote `sklearn/externals/joblib` and broke 60 unrelated tests.
- **Adversarial finalize review.** One-shot execute turns whose completion gate is otherwise
  clear, with a small edit surface (1-4 existing files touched) and verification expected, get
  exactly one extra pass before finalizing: re-read the request, enumerate every implied
  behavior (message wording, boundary inputs, interactions), extend the reproduction to cover
  the gaps, run it. Costs one model response on qualifying turns; turns that only create new
  files are exempt. Kill-switch `SYLLIPTOR_ADVERSARIAL_FINALIZE` (or config
  `adversarial_finalize_review`); event stage `adversarial_finalize_review`.
- **Derived-artifact read guards.** `fs_read` now returns a size + head-sample stub (with an
  explicit reason: dependency lockfile, minified bundle, source map, generated/vendored path)
  instead of full content for machine-generated artifacts, classified by file class in
  `file_classification.derived_artifact_reason` — never by task wording. The model opts in
  with `allow_derived=true` when the artifact itself is the subject of the task; files at or
  under 2 KiB pass through untouched. `fs_read_lines` gains a byte ceiling (default 48 000)
  so a single minified line can never flood the context; when even the first requested line
  exceeds it, a clipped head is returned and marked `line_clipped`. The same-batch read cache
  keys `fs_read` reuse on `allow_derived` and never records byte-capped or stub results as
  complete file content.
- **Tool-necessity, single-answer, and artifact-reporting prompt norms.** The base prompt now
  states: match tool use to need (no tool calls for social/meta turns); one answer per turn —
  continue from pre-tool text, never restart or re-greet; name every created or modified file
  by repo-root-relative path and summarize a produced artifact's substance ("Created `X`"
  alone is not a report); and when asked to create something that already exists, say so and
  either apply the requested content or ask one concise question. Bootstrap-payload budget
  tripwires rebased accordingly (interactive 8450, one-shot 9100 estimated tokens), and the
  conflict-resolver tight-budget fixture window rebased 8192 → 8448 for the same growth.

- **Sticky persona models.** A persona switch resolves its model role through the existing
  chain (`persona_models.<persona>` → role → env/plan/`role_models`/default, profile-filtered)
  and swaps the live chat client when the resolved model differs — per-model cached, base
  client restored on returning to `code`, best-effort with a logged failure event. Default
  installs never swap. The footer model label and `persona_switch_applied` events show the
  live model.
- **Custom personas from `.sylliptor_personas/*.md`.** Frontmatter-defined personas (name,
  description, exec_mode capped at `review`, model_role, allow_write_globs, enabled) with the
  markdown body as an untrusted-marked prompt overlay. Fail-closed loading with startup
  warnings; builtins cannot be shadowed; available in `/persona`, the pickers, and the Tab
  cycle. `switch_mode` proposes builtins only.
- **`--persona` on `run` and `chat`.** One-shot runs get the clamp, write scope, overlay, and
  role model resolved before the session exists; chat gets its starting persona for the
  invocation. Persona docs land in `docs/personas.md` and the README, `/status` shows the
  active persona, both config menus gain a Personas section, and the system prompt carries a
  short persona-contract section when the feature is on.
- **Architect can now write markdown plan documents — and nothing else.** The Architect
  persona moves from readonly to a review default carrying a host-enforced write scope
  (`allow_write_globs: *.md, **/*.md`): `fs_write` to `plan.md` lands (behind the normal
  review approval), `fs_write` to `hack.py` raises "Blocked write outside allowed scope" —
  Kilo's fileRegex idea, enforced by the gate instead of a client-side check. The clamp pulls
  fullaccess users down to review while Architect is active so the scope always binds
  (fullaccess bypasses write scoping by design), a persona defers to any user-set write scope
  and can never widen one, and switching away — or any explicit `/mode <exec>` — restores the
  user's own scope exactly.
- **Persona overlays for Architect, Ask, and Debug.** Non-default personas inject one
  ephemeral system message per turn (classic and TUI run paths) describing the posture —
  Architect plans without editing, Ask answers read-only, Debug reproduces before fixing.
  Every overlay states explicitly that the host owns persona/mode state and execution
  gating: the overlay is a convention; if it and the gate ever disagree, the gate wins by
  construction. The Code persona injects nothing, so pre-persona turns carry byte-identical
  prompts.
- **`switch_mode`: the model can propose a persona switch; the user decides.** A new built-in
  tool, registered only in top-level interactive chat (never in one-shot, forge, swarm,
  subagent, or conflict runtimes, and never when `non_interactive` — automation cannot switch
  personas silently). The proposal surfaces as a normal approval prompt; approving parks the
  switch and the chat loop applies it when the turn ends, so the tool surface is never swapped
  mid-turn and the clamp rule runs from the user's real base mode. Declining returns a plain
  "continue in the current persona" result to the model — not an error — and a repeated
  identical proposal is auto-declined without re-prompting. This is the deliberate inversion of
  the deleted router: posture changes are visible, user-approved state transitions, not silent
  pre-turn predictions.
- **`/persona` switches personas; `/mode` stays the execution-mode command.** Two commands,
  two vocabularies: `/persona code|architect|ask|debug` (bare `/persona` opens a picker in
  classic and TUI) applies the persona's convention — prompt overlay, model role, default
  execution mode, write scope — through the clamp rule: a persona may lower the effective
  execution mode but can never raise it above the mode the user chose, so persona switching
  is not an escalation path. Switching back to `code`/`debug` restores the remembered user
  mode; an explicit `/mode <exec-mode>` always wins, clears the restore point, and `/mode`
  with a persona name points at `/persona` instead of guessing. In the TUI, **Tab on an
  empty input cycles the persona** (Kilo/OpenCode-style) while Tab keeps its completion
  behavior whenever the input has text; cycling is silent — the footer badge flipping
  (`code · safe` → `architect · safe` → …) is the feedback — and the welcome hint line
  advertises the shortcut. Persona switches are refused while Plan Mode is on, and a
  non-default `default_persona` config is applied once at chat startup. With
  `SYLLIPTOR_PERSONA_MODES=off` (or `persona_modes_enabled = false`) `/mode` accepts only
  execution modes, exactly as before.
- **Persona-mode foundations (registry and state only; no UX change).** A persona is a
  named convention — prompt overlay, model role, default execution mode — layered on the
  execution-mode gate, which stays the sole enforcement layer. This lands the registry
  (`code`, `architect`, `ask`, `debug`, with `code` as the exact no-op default), the
  `session.persona` field, a `persona:` environment-context line that appears only for
  non-default personas, the `persona_changed` surface event, and the config surface:
  `default_persona`, `persona_models.<persona>` role overrides resolved through the
  existing role-model chain, and the kill-switch pair `SYLLIPTOR_PERSONA_MODES=off` (or
  `persona_modes_enabled = false`). Switching UX and the model-proposed `switch_mode`
  tool land in follow-up changes; see `docs/persona_modes_design.md`.
- **`/ask <question>` runs one read-only turn.** The session switches to the readonly
  execution mode for exactly that turn — write and shell tools are removed and blocked by
  the host — and the previous mode is restored afterwards, even on errors. This is the
  deterministic replacement for the router's per-turn advisory-posture inference: "just
  look, don't touch" is now something the user states, not something a model predicts.
- **`/chat <message>` answers with a minimal prompt and no tools.** One bounded
  conversational reply from the main model without workspace context — the deliberate
  cheap-small-talk surface now that no router classifies turns automatically.

### Changed

- **Provider reasoning controls are now verified at the exact request boundary.** Groq,
  supported Cerebras models, Cohere, DeepSeek, and Moonshot/Kimi use the existing per-model
  wire contract as the sole emission allowlist, including documented explicit `none` values.
  The Kimi membership endpoint keeps its surface-specific contract. xAI's legacy Chat
  Completions reference and Fireworks' MiniMax M3 documentation do not verify their exact
  flat request values, so those routes remain safely unenrolled instead of receiving guessed
  controls.
- **Live NVIDIA catalog entries disclose unknown chat compatibility.** Native capability
  metadata still excludes explicitly non-chat models. Minimal OpenAI-style model objects are
  now represented honestly as unverified instead of being guessed from English model-name
  fragments; setup validates a selected entry, and custom model entry remains available.

- **Assistant prose no longer bypasses the completion evidence gate.** Question punctuation,
  response length, language, and the inactive `blocked: … category: …` prose convention no
  longer manufacture accepted blockers or skip verification, repro, regression, acceptance,
  and blast-radius checks. The first-turn grounding retry and one-shot clarification retry are
  removed; personas and execution modes remain the explicit posture controls. Event-schema
  cleanup removes `normal_chat_first_turn_repo_execute_retry`, the
  `one_shot_clarification_advisory` and `clarification_suppressed_by_guard` intervention details,
  the `clarification_requested` problem/stage, and the `clarification_response` /
  `clarification_allows_completion` payload fields.

- **Turn meaning now comes from one provider-neutral semantic router contract instead of fixed
  language patterns.** Router schema v3 records the requested outcome, task shape, conversation
  relation, minimum effects, grounded targets, ambiguity, and complexity. The host validates that
  structure, derives execution posture from it, and enforces effects at tool-call time; permission,
  sandbox, and approval checks remain host-owned. `SYLLIPTOR_SEMANTIC_TURN_CONTRACT=off` (or
  `semantic_turn_contract_enabled = false`) temporarily selects the existing posture-only
  compatibility path without restoring the deleted natural-language classifier. Image-attached
  turns use the same semantic router when a current router client is available; embedders that
  replace only the main client still avoid stale-router calls.

- **The unified turn path is now the default: no router model call runs before a turn.**
  Every text turn goes straight to the main model with the full per-mode agent surface, and
  execution posture derives from the execution mode (`readonly` stays advisory; other modes keep
  the full execution contract). Repository turns — including every subagent, Forge worker, and
  swarm dispatch — lose the router's per-turn preflight (roughly 3.5–5k input tokens and one
  serial round trip before the main model could start), and the router's whole failure class
  (transport retries, contract repair, degradation rules) disappears from the critical path.
  The honest trade: casual small talk now pays the full agent prompt, which provider-side prompt
  caching makes cheap on cached providers; on providers without prompt caching, `/chat` is the
  explicit cheap-conversation surface. `SYLLIPTOR_UNIFIED_TURN_PATH=off` (or
  `unified_turn_path_enabled = false`) selects the legacy routed path for one release while the
  router is removed. On the unified path the router-contract consumers run on observed
  facts instead of predictions: the pinned task brief updates from approved-plan submissions and
  from instructions that actually produced material edits (follow-up chatter can never clobber
  it); the reproduction-first protocol is offered as a conditional directive on execute-capable
  turns and its completion gate binds exactly when a failing pre-fix reproduction was observed;
  and the optional new `reply_language` config replaces router language detection for reply
  steering and final-summary rewrite.

- **The skills-eval launch report no longer tracks an execution-posture fallback rate.** The
  metric counted router `route_decision` events, which the default turn path no longer emits;
  keeping it would have reported a permanently-zero rate as if it were a healthy signal.

### Removed

- **`/chat` is retired in favor of the Ask persona.** The command now prints a pointer to
  `/mode ask` (persistent read-only Q&A) and `/ask <question>` (one read-only turn) and is
  removed from the completer, visible-command list, and help panels. The internal
  `chat_only` run-turn plumbing (`CHAT_ONLY_SYSTEM_PROMPT`, the `chat_only` request field)
  remains accepted-and-ignored for one release with no producer, then will be deleted.
- **The pre-turn semantic router is gone.** `agent/routing.py`, the router client, the
  semantic-contract validation/repair pipeline, route arbitration, the non-repo reply
  short-circuit, the per-turn semantic effect guard, and the `routing_llm` usage operations
  no longer exist; nothing classifies a turn before the main model sees it. Authorization
  follows the actual proposed action through execution modes, approvals, and the sandbox.
  `routing_mode`, `route_arbitration_enabled`, `semantic_turn_contract_enabled`, and
  `unified_turn_path_enabled` remain accepted-and-ignored for one release. Router-only
  session events (`route_decision`, `route_contract_unavailable`) are no longer emitted;
  old session logs still replay.

- **Router configuration surfaces are gone from every UI.** The `/config` "Routing" section
  (classic menu and TUI), the setup wizard's router-model step (terminal and TUI), and the
  VS Code extension's "routing model" quick-pick no longer exist; `sylliptor doctor`'s
  reasoning-suppression probe now targets the profile's default model. Legacy
  `role_models.router` entries load fine and are dropped on the next profile switch.

### Fixed

- **Provider URL inference has one shared endpoint classifier.** Request shaping, model
  metadata, and provider throttling now agree on provider-owned hosts and exact paths while
  preserving their separate unknown-host fallbacks. This also distinguishes `api.kimi.com`'s
  membership contract from Moonshot's platform API.
- **Current provider metadata records its official provenance.** The live-source overlay now
  carries the official provider documentation URLs used for its facts, and Gemini 3.7 Flash,
  3.6 Flash, and 3.5 Flash-Lite use Google's documented 1,048,576-token input limit. Gemini
  3.7 and the verified introductory 2026 pricing remain in place following their GA release.
- **Provider-owned model identifiers no longer get pinned to stale local aliases.** Active Gemini
  2.5 IDs, Gemini `*-latest` aliases, and Mistral's `mistral-medium-latest` alias pass through to
  their providers unchanged; only shut-down or invalid legacy IDs are migrated. The Mistral
  preset now uses the documented
  `mistral-medium-3-5` API ID and preserves the former `mistral-medium-2604` default as a backward-
  compatibility alias.
- **First-turn repo grounding no longer overrides conversational hand-backs.** In interactive
  chat, the first-turn grounding retry used to fire on any text-only reply because turn
  intent is mode-derived ("execute" for every write-capable mode), so a plain "hi" produced a
  greeting, a forced repo inspection (README, manifests, `package-lock.json`), and a second
  greeting. The retry now skips replies the existing clarification-shape check classifies as
  hand-backs (question shape + no repo tool activity + provider-phase veto — structural, per
  the behavioral-turn-gates doctrine; no message vocabulary), and the question-shape check
  recognizes the unambiguous non-ASCII question marks (U+FF1F, U+061F, U+037E, U+055E; ASCII
  ";" stays excluded so trailing code fragments cannot masquerade as questions).
- **A grounding retry keeps the already-streamed reply in the transcript.** The retry path
  previously re-ran the step without appending the first reply to `self.messages`, so the
  model — never having seen its own words — restarted the answer and the user saw it twice.
  The retry now appends the assistant message first (mirroring the continuation-nudge path)
  and the nudge text instructs continuing from the visible reply, never restating it.
- **The active persona survives `/resume`.** Resume restores the base execution mode from
  `session_start`; the last `persona_switch_applied` event now re-applies the persona on top,
  reproducing the narrowed mode, markdown write scope, restore point, and sticky model exactly
  as the clamp left them — in both the classic `/resume` flow and the TUI resume picker.
- **The TUI no longer shows a reply twice after a mid-stream provider retry.** When a
  streamed request failed after tokens had already rendered, the transport retry re-ran the
  request and the second generation streamed into the same live block — the pane ended up
  showing "generation A + generation B" glued together, and because retries produce slightly
  different wording ("your repo" vs "the repo") the existing verbatim dedupe could never
  catch it. `finish_assistant` now snaps the block to the final reply whenever the block
  ends with it but carries an abandoned prefix (the final text is exactly what the session
  history keeps, so nothing real is lost); a ≥25% length guard keeps a pathological
  tail-fragment final from truncating a legitimate answer. Content-based and
  provider-agnostic, like the existing duplicate-answer collapses.

  The doubling is now also erased **live**, not just at the end: the chat loop's delta
  callback carries a duck-typed `stream_restart` reset channel. When a streamed request is
  retried after tokens already rendered, the client fires the hook and the TUI wipes its
  live block before the retry restreams (classic terminals print a "Provider retry —
  restarting the reply…" marker instead, since ink can't be unprinted). The same reset runs
  before an empty-response-stall recovery call, covering providers whose final aggregation
  loses the content they just streamed. And because the surface can now erase partial
  output, the client's "never replay after partial output" guard relaxes exactly when the
  reset channel is present — mid-stream transport failures that previously killed the turn
  with "LLM stream interrupted after partial output" now retry seamlessly; without the
  channel the old fail-safe behavior is unchanged.
- **Typographically faithful router evidence no longer turns valid work into an unknown advisory
  fallback.** Grounding comparisons now tolerate Unicode composition, whitespace, newline, and
  curly-quote normalization while preserving the router's original text. Individual ungrounded
  quotes and targets are dropped and counted at the injection boundary; a known outcome still
  fails closed when no evidence survives. The one bounded repair request also retains the complete
  routing rules and workspace context instead of replacing them with a partial repair prompt.
  Workspace-target extraction also keeps extensionless dotfiles such as `.npmrc` available to
  verification selection.

## [0.13.1] - 2026-08-27

Input usability and runtime reliability improvements.

### Fixed

- The TUI now keeps input geometry, selection editing, paste-token hints,
  dragged-selection scrolling, and terminal-theme styling consistent across
  chat, configuration, setup, and workspace-guard surfaces.
- Intentional clean stops now exit successfully and retain a machine-readable
  `stop_reason`; genuine failures and unknown stop reasons remain non-zero.
- Connections dropped mid-response receive a dedicated bounded retry schedule,
  preserve caller-configured larger budgets, respect wall-clock caps, and emit
  consistent retry, stream-restart, and terminal diagnostic reasons.

## [0.13.0] - 2026-08-22

The reliability wave. Six stacked fixes (PR1-PR6) targeting the failure modes
that cost the Terminal-Bench 2.1 and SWE-bench campaigns their reproducibility
and burned roughly 30% of compute on reward-0 tasks — most visibly a
`NonZeroAgentExitCodeError` on `compile-compcert` in every trial. The seventh
change (PR7) adds only verification scripts, an operator re-run protocol, and
this changelog; it makes no agent-behavior change. Every model-facing string
this wave introduces is enumerated in the audit subsection below.

### Added

- **PR1 — write-path secret redaction** (`logging_redaction.py`). Credential-bearing
  environment values (names ending `_API_KEY` / `_TOKEN` / `_SECRET` / `_PASSWORD`,
  plus credential-shaped `SYLLIPTOR_*` values) and risky bearer/assignment patterns are
  replaced with a placeholder (`«redacted:<NAME>»` / `«redacted:pattern»`) at every
  write boundary — session store, crash-diagnostics log, provider-telemetry sink, and
  research artifacts — so a live API key can no longer reach a log on disk. Idempotent by
  construction. No model-visible strings.
- **PR2 — run-budget enforcement and clean exit codes** (`budget_policy.py`). A
  non-interactive run resolves a wall-clock budget (`DEFAULT_RUN_BUDGET_SECONDS` = 3600s,
  overridable with `SYLLIPTOR_RUN_BUDGET_SECONDS`) and a `BudgetWatchdog` trips a
  cooperative cancellation at `budget + grace` (`SYLLIPTOR_BUDGET_GRACE_SECONDS`, default
  60s) regardless of what an in-flight blocking call is stuck on — the stop gate the
  engine's pre-existing start gates never had. A budget stop is now a *normal* outcome:
  `stop_reason: "run_budget_exhausted"` is written to the session-log `final` event and to
  the crash-diagnostics `run_finished` event, and the process exits `0` instead of raising
  `NonZeroAgentExitCodeError`. One no-progress `ProgressCheckpoint` fires at
  `SYLLIPTOR_BUDGET_CHECKPOINT_FRACTION` (default 0.33) when a run has recorded no material
  edit or verification, emitting a `progress_checkpoint_failed` event and one model-visible
  notice.
- **PR3 — budget-preemptible shell waits and dispatch telemetry** (`dispatch_timing.py`).
  A `shell_wait` blocked on a quiet long-running process now observes the budget watchdog
  and unblocks within ~0.5s of cancellation instead of up to 60s, reporting
  `wait_interrupted_by_budget` rather than discarding collected output. Every shell result
  carries a `dispatch_overhead_seconds` measurement, also rolled into the deadline's
  `duration_observations`, so time spent in dispatch versus in the command is visible. No
  model-visible prose strings (two new result keys; see the audit note).
- **PR4 — durable persistent services and non-interactive shell defaults**
  (`service_persistence.py`). `shell_background` gains `persist` and `probe_port`
  parameters: `persist: true` routes the start to the durable-service manager, which
  detaches the child into its own session with file-backed stdio so it survives turn
  finalization and session close; `probe_port` (explicit, or inferred from the command
  line) is probed for TCP liveness. Non-interactive defaults (`DEBIAN_FRONTEND=noninteractive`,
  `GIT_TERMINAL_PROMPT=0`, `PIP_NO_INPUT=1`, closed stdin) are filled in without overriding a
  task that set them. At finalization a single dead-service notice fires if any persist-mode
  service is no longer healthy. Emits a `service_start` event carrying the recorded liveness.
- **PR5 — edit discipline: wholesale-rewrite, thrash, and scratch-file guards**
  (`edit_discipline.py`). Three warn-only guardrails, none of which blocks or rewrites a
  model action. A full-file `fs_write` that changes more than 80% of a file's lines, or that
  is serialization-only (same meaning, different bytes — the `filter-js-from-html` signature),
  attaches a rewrite warning to the tool result under a `warning` key. Eight near-identical
  failed attempts on one action family (path/command with its numeric tail removed) emit a
  bounded thrash progress notice (at most two per run). Scratch files left in the tree are
  identified for the local finalization summary (that summary is a runtime artifact, not
  model-visible). Emits a `scratch_files` signal.
- **PR6 — sampling determinism and build/run provenance** (`build_identity.py`,
  `run_provenance.py`). `SYLLIPTOR_SAMPLING_TEMPERATURE` / `_TOP_P` / `_SEED` pin sampling on
  the wire request through the single `apply_sampling_to_payload` chokepoint (unset means the
  payload is left byte-identical to the pre-PR6 transport). Each provider response's `model`,
  `system_fingerprint`, and an allowlisted, deny-listed set of headers are captured and rolled
  into a per-run drift verdict. A non-fakeable build identity — commit, UTC build timestamp,
  and dirty flag — is stamped by `scripts/generate_build_info.py`, printed by
  `sylliptor --version`, and can gate a benchmark start via `SYLLIPTOR_REQUIRE_CLEAN_BUILD` /
  `--require-clean-build`. A once-per-run `config_snapshot` event records the effective config
  (secrets structurally masked as `[secret]`), the build identity, and the sampling settings.
  No model-visible strings.

### Model-visible string changes in 0.13.0

This subsection is the single audit artifact of the wave's model-facing
footprint: every string or result key that reaches the model in a request
(tool-schema text, a tool-result value or key, or a system/nudge message the
model reads). It was cross-checked against `git diff main...` for the whole
wave. Each string is quoted verbatim and matches its source constant exactly.

**Originally enumerated (confirmed present, verbatim, and correctly classified):**

1. **PR2 budget checkpoint notice** — `budget_policy.BUDGET_CHECKPOINT_NOTICE`, injected as
   an ephemeral system message when the no-progress checkpoint fires:

   > Budget checkpoint: no material progress recorded yet. Reassess approach or report the concrete blocker.

2. **PR4 `persist` and `probe_port` tool-schema parameters** — on the `shell_background`
   schema (`tools/registry.py`). Parameter descriptions the model reads:

   > persist: Keep the process running after the session ends, as a durable service.

   > probe_port: TCP port to probe for liveness; inferred from cmd when omitted.

3. **PR4 dead-service notice** — `service_persistence.SERVICE_CHECK_NOTICE_TEMPLATE`, injected
   as an ephemeral system message at finalization:

   > Service check: process started as a persistent service is no longer running: {summary}. Restart it or note why it is not needed.

4. **PR5 rewrite warning** — `edit_discipline.REWRITE_WARNING_TEMPLATE`, carried on the
   `fs_write` result:

   > warning: full-file rewrite of {path}: {pct}% of lines changed. If the task requires preserving formatting of untouched regions, prefer a targeted edit.

5. **PR5 thrash notice** — `edit_discipline.THRASH_NOTICE_TEMPLATE`, injected as an ephemeral
   system message:

   > Progress check: {n} similar attempts on {family} without a passing result. Synthesize what you know into a final answer or a concrete blocker report now.

6. **The `warning` result key** — a new top-level key on the `fs_write` tool result
   (`agent/tools_assembly.py`), whose value is the PR5 rewrite warning above.
   (Distinct from the session-log `warning` *event type*, which is not model-visible.)

**Audit note — model-visible surface BEYOND the six above (found by the PR7 grep audit).**
The originally-enumerated list was not complete. PR4's persist path and PR3's shell results
expose additional model-visible content that belongs in this audit:

- **PR4 persist success-path result payload.** On a successful `shell_background(persist: true)`
  the tool result carries a new string value and new keys the model reads: `"lifetime": "durable"`,
  and the keys `persist`, `pid_alive`, `port_listening`, and `liveness`. The `liveness` value is
  built from these fragments (`service_persistence.LivenessReport.describe()` /
  `describe_port_probe`) — the same four fragments the dead-service notice's `{summary}` embeds:

  > pid alive

  > pid not running

  > listening on :{number}

  > nothing listening on :{number}

- **PR4 durable-service error strings**, returned to the model as the tool-result `error` value on
  the failure paths of `shell_background(persist: true)` / durable-service start:

  > Invalid durable service request: {exc}

  > Failed to start durable service: {exc}

  > probe_port must be an integer between 1 and 65535

- **PR3 result keys** (new key names the model sees; values are numeric/boolean, no new prose):
  `dispatch_overhead_seconds` (float) and `wait_interrupted_by_budget` (bool).

**Confirmed to add NO model-visible strings:** PR1 (redaction operates only on log/telemetry
text), PR6 (all output goes to `config_snapshot` / telemetry / `--version` console, never the
conversation). PR3 adds no prose string, only the two result keys noted above. The pre-existing
deadline convergence/wrap-up system prompts (`_DEADLINE_CONVERGENCE_SYSTEM_PROMPT_TEMPLATE` and
siblings in `agent/turn/core.py`, "Run budget checkpoint: about {elapsed_percent}% …") shipped in
0.9.8 and are byte-identical in this wave — they are **not** a 0.13.0 change.

## [0.9.8] - 2026-07-31

### Added

- **The IDE bridge can delegate native VS Code Tasks and Debug lifecycles to a negotiated host.**
  `tasks.list/run/status/terminate` and `debug.list/start/status/stop` use typed, bounded host-action
  requests instead of terminal scraping. Requests bind the exact session, workspace root/scheme/
  authority, immutable fence, cancellation state, and response size; starts require a fresh
  approval in every mutating mode, read-only and untrusted contexts fail closed, and session close
  cleans up only the executions it owns.
- **An application that spawns the CLI can now read auth state instead of parsing prose.**
  `sylliptor auth status`, `sylliptor auth list` and `sylliptor whoami` accept `--json` and emit
  exactly one JSON object on stdout. Human output stays the default.
  - **The exit code describes the command, not the answer.** `0` means the command ran — whatever
    the auth state — and the payload carries `authenticated`. A nonzero exit means the command
    itself failed (`2` for a usage error such as an unknown connection id). Previously `1` meant
    both "not authenticated" and "something went wrong", which a supervising app could not tell
    apart. The human surface keeps its existing shell-friendly exit codes.
  - Each object carries `connection`, `authenticated`, `account_label`, `method`, `detail`,
    `transport` and `error`. A provider or runtime error becomes data (`authenticated: false` with
    the reason in `error`) rather than a crash, so a status probe always answers. `auth list --json`
    returns the same per-connection shape inside a `connections` array.
  - **`sylliptor doctor auth [--json]`** reports the environment behind a disagreement: resolved
    home and config dir (plus `SYLLIPTOR_CONFIG_DIR`), whether PATH looks truncated the way a
    GUI-spawned process often sees it, the keyring backend with a non-mutating availability probe,
    per-store credential health — which key source each store was written with and which the next
    write would use — and whether the context looks interactive or spawned.
- **The credential store says when the OS keyring is unavailable instead of degrading quietly.**
  Every auth status payload carries `keyring_available`, `keyring_backend` and
  `credential_fallback` (`dpapi` or `filesystem-random`), the human surface prints a matching
  notice, and the store logs one warning per distinct degradation rather than a debug line. The
  existing DPAPI and key-file fallbacks are unchanged; only their visibility is.
- **An interrupted run recovers itself instead of blocking every retry.** A killed or crashed
  execution used to leave a workspace that refused to run again: an ambiguous lock file that only
  said "clear the lock only if it is definitely stale", and a run pointer that still claimed the
  run was going. Recovery is now built in, and the outside workaround of rewriting
  `.sylliptor/current_run.json` by hand to un-stick a run is no longer needed.
  - **The run pointer carries an explicit lifecycle.** `current_run.json` (`schema_version` 5)
    records a `status` from a closed enum — `draft`, `approved`, `running`, `interrupted`,
    `completed`, `failed` — plus `status_history`, the `run_owner` (pid/host/mode) while running,
    and a `plan_fingerprint`. `draft` ↔ `approved` tracks the execution-readiness gate, so a UI can
    show "ready to run" without re-deriving it. Pointers from older builds have no status and read
    as `draft`.
  - **Crash leftovers heal on read.** A run marked `running` holds the workspace lock for its whole
    life, so `running` with no live lock describes a process that is not there. Any command that
    opens the run transitions it to `interrupted` automatically, so no surface shows a phantom
    active run. `forge status`/`forge show` print it and carry `run_status`, `run_status_reason`,
    `run_owner` and `resumable` in their `--machine` payloads.
  - **Locks now publish a heartbeat, not just a pid.** The owner refreshes a timestamp every
    `heartbeat_interval_s` (default 15s) and promises to keep it fresher than `heartbeat_ttl_s`
    (default 120s). A **same-host** lock is recovered automatically when its pid is gone *or* its
    heartbeat has lapsed — which is what unblocks the case a pid check cannot see, a recycled
    process id. A heartbeat-expired lock is re-read after one grace interval first, so a machine
    returning from sleep gets to prove it is alive, and a lock written before the contract existed
    is never reaped by age because it never promised to beat. Ambiguous and other-host locks still
    fail closed; their error now names the lock's age, heartbeat age, owner, host, pid, the
    staleness verdict, and the exact command that clears it. Tunable via
    `SYLLIPTOR_RUN_LOCK_HEARTBEAT` / `_HEARTBEAT_INTERVAL_S` / `SYLLIPTOR_RUN_LOCK_TTL_S`.
  - **`sylliptor forge unlock [--force] [--machine]`** inspects the workspace and run locks,
    explains each verdict (`stale`, `active`, `ambiguous`) in words, clears the provably stale ones
    and reconciles the run status afterwards. Exit `0` when the workspace ends up unlocked, `1`
    when a lock was kept because its owner could still be alive. `--force` clears an ambiguous lock
    with an explicit warning that a live owner would now have a second mutator.
  - **`sylliptor forge resume`** picks an interrupted (or failed) run back up from its last
    incomplete task. It revalidates the plan against the fingerprint recorded when the run started
    and, if the plan drifted while nothing was running, stops and lists exactly *what* drifted
    (`project goal changed`, `T02 changed: acceptance criteria`, `tasks added: T04`) rather than
    silently continuing or silently resetting the run to draft; `--reapprove` accepts the current
    plan. Tasks the dead process left `in_progress` — a status that is deliberately not runnable —
    are re-armed to `interrupted` so they can be retried. Execution itself is `forge run`, so every
    `forge run` flag applies; `--dry-run` reports what would be resumed and exits.
- **`sylliptor forge run` — the sequential path is now the blessed way to execute a plan.** Every
  ready task, in dependency order, one at a time, in the main checkout: no per-task worktrees, no
  parallel workers, no batch integration gate. Each task is a branch → execute → commit → verify →
  (review) → merge cycle in the checkout you are sitting in, so the next task starts from the
  merged result and per-task verification actually checks the integrated tree. The run stops at the
  first failure by default (`--keep-going` opts out), leaving a state you can inspect and resume
  from. `--dry-run` prints the simulated execution order — dependencies unlocking task by task —
  without running anything.
  `forge run` and `forge exec` now share one execution core (`execute_forge_task`), so scope
  triage, verification, the repair loop, the review gate and the PR flow cannot drift between
  them: `forge run` is exactly `forge exec` applied to each ready task. Both write the usual
  per-task artifacts; `forge run` additionally writes one linear
  `execution/sequential_run.jsonl` narrative and an `execution/sequential_summary.json` outcome,
  and under `--machine` emits the same ordered NDJSON event stream as every other Forge command
  (one terminal event, `run_completed` carrying the run outcome).
  PR flow is the default because it is what makes per-task verification possible; a workspace with
  no git HEAD degrades to `--no-pr` with a warning and a `verification_unavailable` event, while an
  explicit `--pr` there still fails rather than being silently ignored.

### Changed

- **Session listings say what they hid instead of filtering silently.** `sessions list` and
  `sessions score` scope to the local account by default, which made "my session is gone"
  indistinguishable from "your session is filtered" — an empty list looked the same either way.
  Both now print how many sessions from other accounts were hidden whenever anything was dropped,
  including when that leaves the list empty, and both accept `--all-owners` to include them
  (`sessions list --all` stays as an alias).
- **A merge conflict no longer starts a resolver agent on the sequential path.** `forge exec --pr`
  used to fall into the worktree-based conflict auto-resolver — a second full agent run in a
  dedicated conflict worktree — turning "one task did not merge" into an opaque second execution.
  It now stops and reports: the branches involved, the conflict review artifact, the exact commands
  to land the merge by hand, and how to resume the remaining tasks. Auto-resolution is opt-in via
  `--auto-resolve-conflicts` on `forge exec` and `forge run`. `forge swarm` is unchanged and still
  auto-resolves by default, because parallel task branches genuinely conflict with each other.
- **`forge swarm --parallel 1` delegates to the sequential engine.** A swarm of one has no
  parallelism to exploit, so it now runs `forge run` in the main checkout and says so, instead of
  provisioning worktrees for nothing. It delegates only when nothing swarm-specific was requested:
  `--dry-run`, `--keep-worktrees`, `--retry-merge-conflicts`, an explicit `--integration-verify`,
  or an active `--replan` mode keep the swarm engine, and the reason is printed rather than the
  capability being silently dropped. `SYLLIPTOR_SWARM_SEQUENTIAL_DELEGATE=0` disables delegation.
- `forge swarm` is documented and helped as the parallel optimization rather than the normal way
  to execute a plan, and `docs/forge.md` leads with `forge run`.

### Fixed

- **A placeholder keyring backend no longer produces a credential store nothing can decrypt.**
  `keyring.backends.null` — which this project's own macOS CI selects via
  `PYTHON_KEYRING_BACKEND` — accepts a master-key write and silently discards it. The store trusted
  the write, stamped the envelope `key_source: keyring`, and the next process could not find the
  key. Writes are now read back and a backend that does not persist them falls back to DPAPI or a
  private key file, so credentials survive across processes in headless and spawned contexts.
- **A ChatGPT Codex status probe no longer raises.** `account_status()` guarded the token refresh
  but not the credential-store read that precedes it, so an unreachable keyring crashed every
  caller polling for account state. Both are guarded now, and an expired session reports the stable
  `detail` value `session expired` for a supervising app to branch on.
- A task whose files were written successfully is no longer failed because the workspace has
  nothing to verify with, and a task whose verification actually failed now gets a chance to fix
  it. Strict verification collapsed two unrelated facts into one verdict: `verify_gate`
  deliberately suppresses generic fallbacks and returns *no* commands for docs-only, static-web,
  CI-only and Terraform/Compose workspaces, and Forge turned that absence into
  `not merged: strict verification unavailable` — a plain failure — after which swarm cleanup
  deleted the worktree. Missing tooling was reported as broken work, and then the work was thrown
  away.
  (A) **New outcome `completed_unverified`.** When writes succeeded and no authoritative
  verification command exists, the task completes instead of failing: exit code `0`, a
  `task_completed` event, a `verification_unavailable` event carrying
  `outcome: "completed_unverified"`, and the status counts as terminal-successful for dependent
  tasks and for every done/failed tally. Nothing merges either way — in `forge exec --pr` the work
  stays committed on the task branch, the branch is never deleted, and the working tree returns to
  the base branch for review; in `forge swarm` the task's branch is left unmerged and its worktree
  is deliberately *not* cleaned up. Knowledge capture from such a task is explicitly *not* promoted
  as validated, because nothing validated it. `--verify warn` and `--verify off` are untouched:
  they never failed for this, so they keep completing as `done`.
  (B) **A failing strict verification is repaired, not just reported.** The failing command's own
  output — the tail, where the assertion is, rather than the collection noise — is fed back to the
  same executing agent through the same invocation machinery (same config, mode, write guard, step
  budget and MCP scope), the fix is committed to the task branch, and verification is re-run, for
  up to `--verify-repair-attempts` attempts (default 2, `0` disables, capped at 10;
  `SYLLIPTOR_VERIFY_REPAIR_ATTEMPTS` sets it session-wide). The loop stops early when verification
  passes, when an attempt changed nothing (re-running the same commands would produce the same
  failure), when the repair agent exits non-zero, or when the failure is infrastructure-unavailable
  rather than something code can fix. Each verification pass is snapshotted separately, so a repair
  attempt's own committed edits are never misread as verification commands mutating the repository.
  Every attempt is recorded in the report, in the terminal event under `verification_repair`, and
  as `verification_result` events labelled `repair.<n>`. This is the `forge exec --pr` flow only;
  swarm workers still surface a failed verification for `--retry-failed` rather than repairing
  in-run.
  (C) **No work is destroyed by cleanup.** Before *any* worktree or branch cleanup of a failed
  task, the swarm writes that task's full diff — committed and uncommitted, plus the contents of
  untracked files, which is exactly the work that existed nowhere else — and its verification log
  to `.sylliptor/runs/<run_id>/execution/evidence/<task_id>/`. That is inside the run, not the
  worktree, so cleanup then proceeds exactly as configured and `--keep-worktrees` keeps its
  meaning. Capture never mutates the workspace it reads (no staging, no index writes), never
  raises, and never blocks the cleanup it precedes; anything it could not read is recorded in
  `evidence.json` under `errors` instead of being dropped.
  `--verify off/warn/strict` are unchanged, and strict still blocks the merge on a real
  verification failure once the repair attempts are spent.

- A change is no longer allowed to finish without anyone having looked at what else it broke. In 23
  of 134 observed failures the shipped patch broke existing tests, and in 8 of those the fix itself
  was *correct* — only the collateral damage failed the task. The worst two took out 188 and 214
  existing tests by editing shared code too broadly. The agent verified its own fix every time; it
  simply never ran anything that would have noticed. Sylliptor now measures the blast radius as a
  fact, on the same observational contract as the rest of the verification protocol — the host
  selects the scope and reads what the agent ran, and never runs anything itself.
  (A) **The tests around a change are selected, not left to the agent's judgement.** From the paths
  the change touches, Sylliptor picks the test file that mirrors each one by name, every test file
  that statically imports a touched module (a real import scan — `import a.b`, `from a.b import c`
  — resolved through the package root so a `src/` layout gives `pkg.mod`, not `src.pkg.mod`), tests
  sitting in the same directory, and tests under the touched package's mirrored test directory.
  Selection is ordered nearest-first and language-idiomatic (`test_x.py`/`x_test.py`,
  `x.test.ts`/`x.spec.js`/`__tests__/`, `x_test.go`). Direct importers only: widening to the
  transitive closure would pull in most of a repository.
  (B) **The scope is baselined on the clean tree, so pre-existing breakage is never blamed on the
  patch.** A run counts as a baseline exactly when no pre-existing repository path has been
  modified yet — writing a new test does not close the window, and a run made *after* product code
  changed is never graced into a baseline, because by then the fix may already be finished and
  crediting it would mask the very breakage this catches. Baselines compose: every clean-tree run
  observed the same unpatched tree, so a whole-suite run before editing baselines any scope, and
  attribution is decided per failing test against what the clean tree actually covered. A failure
  the baseline never ran is reported unattributed — never guessed into either column.
  (C) **New failures block, and mass breakage changes the instruction.** Tests that passed in the
  baseline and fail after the change rank as contradicted, alongside a detected regression, with a
  nudge naming them and asking for a repair that keeps the fix — prefer narrowing the change over
  adding to it, and never edit, skip or delete the broken tests. Past a configurable threshold
  (default 20 newly-broken tests) the instruction switches: that many failures from one change means
  the change is over-broad, so it is reverted and rewritten narrowly rather than patched up test by
  test. A scope that was never re-run after the fix blocks separately, ranked below the verification
  stages so "you ran no tests at all" keeps owning the repair loop when both are true.
  (D) **The blast radius is always reported, and the scope always runs.** Every turn that changed
  existing code ends with a line stating what else was checked — clean, unattributed, or broken with
  the failures listed and the result marked as shipping with known breakage. Uncleared regressions
  are never silent. A scope run that exceeds its wall-clock cap (default 300s) shrinks to its
  nearest tests rather than being skipped, never to nothing, and both the shrink and the file cap
  (default 40) are stated in the summary rather than passing as full coverage. The chosen scope,
  its baseline and its gate result are recorded in the session.
  Kill-switch `blast_radius_gate_enabled` / `SYLLIPTOR_BLAST_RADIUS=off` (default on) — scope runs
  are still captured as telemetry, but the directives and the gate policy revert. Knobs:
  `blast_radius_max_scope_files`, `blast_radius_scope_seconds_cap`,
  `blast_radius_over_broad_threshold`.

- A bug fix is no longer allowed to finish without ever demonstrating the reported symptom. The
  dominant autonomous-run failure was a plausible patch that misses the actual bug: the agent read
  the report, formed an interpretation, changed code, wrote a test for *its own interpretation*,
  watched that test pass, and finalized confident it was done — 62 of 134 observed failures. Every
  signal in the loop agreed, because every signal derived from the same reading of the report.
  Nothing ever exercised the symptom the reporter described. Sylliptor now requires one fact the
  agent cannot produce from its interpretation: a reproduction that failed before the change and
  passes after it. Four changes, none of them conditional on a repository, a task, or a model.
  (A) **A reported symptom gets reproduced before product code is touched.** A task that reports
  defective behaviour — recognized by the existing bug-fix predicate plus the ordinary shapes of a
  defect report (a pasted traceback, "steps to reproduce", "returns X but the expected output is
  Y") — opens with a directive to derive a minimal reproduction of the *exact* symptom, preferring
  any concrete code block the report quotes, and to run it. Feature, docs, refactor and read-only
  turns are classified out and behave exactly as before.
  (B) **A reproduction that passes on the unpatched tree is treated as a wrong interpretation.**
  It must fail first; if it does not, that is evidence the reading of the report is wrong, and the
  agent is told to revise the reproduction rather than start editing — bounded to two rounds so a
  symptom the environment genuinely cannot expose can still be reported honestly instead of
  looping. Pre-fix versus post-fix is decided mechanically: a run is pre-fix exactly when no
  pre-existing repository path has been modified yet, so writing the reproduction (and any new
  test alongside it) does not count as starting the fix, and a reproduction authored *after* the
  patch is correctly recorded as never having observed the unpatched behaviour.
  (C) **The completion gate holds the run to the reproduction.** A bug-fix turn that changed
  product code without a reproduction that failed before and passes after cannot finalize: the
  gate nudges with the concrete deficit (never run / never reproduced / never re-run / still
  failing / passes but never failed) up to the same bounded round limit as the regression and
  expectation deficits. A reproduction still failing after the fix ranks as contradicted, the same
  as a detected regression. Weakening or deleting the reproduction to make it pass is called out
  explicitly, and editing it after the fix is recorded and surfaced.
  (D) **The reproduction's status is always reported, and its scaffolding never ships.** The final
  summary of a bug-fix turn states plainly that the reproduction failed before the fix and passes
  after it, or exactly which of those could not be shown — success is never reported silently on an
  unvalidated symptom. Reproduction files still present in the working tree at finalization block
  with a named cleanup nudge, so the delivered diff carries the fix and nothing else.
  Kill-switch `reproduction_first_enabled` / `SYLLIPTOR_REPRODUCTION_FIRST=off` (default on) —
  reproduction runs are still captured as telemetry, but the directives and the gate policy revert.

- Strict write-scope enforcement rejected the whole task on any undeclared file, so a worker that
  legitimately added a sibling test file lost all of its work to
  `Task was blocked due to strict scope isolation` — the plan's `write_scope` had simply not
  predicted a filename, and there was no way to say so short of failing and replanning. Strict mode
  now triages instead of rejecting. Each out-of-scope change is classified as **adjacent** (a file
  the task created in a directory `write_scope` already reaches, a sibling test file, or a
  generated artifact of a declared file), **protected** (`.sylliptor/`, `.forge/`, `.git/`, or
  anything outside the workspace) or **unrelated** (everything else). Adjacent changes are accepted:
  the path is added to the task's `write_scope`, recorded in the report's new `Scope Amendments`
  section and in a `scope_amended` machine event, and the task continues. Protected paths always
  block and are never offered a scope patch. Unrelated changes block exactly as before, but the
  error now carries the full classified list and a ready-to-paste set of `write_scope` patterns, so
  a human or the replanner can fix the plan in one step instead of re-deriving it. Editing an
  existing neighbouring file is deliberately not adjacent — the directory rule applies only to files
  the task created. The companion "expected local file update was not produced" rejection now
  distinguishes a task that did nothing (still a failure) from one whose changes were all adjacent
  (a pass, with the amendment recorded). `--scope warn` and `--scope off` are unchanged: they report
  and amend nothing. Planner guidance for acceptance rules R3/R4 now states that directory-level
  globs are preferred over exact file lists, which is what makes the adjacency rules land.

- A planner response that failed validation was reshaped by the host instead of being sent back
  to the model. Only one parse was ever strict: on failure the host guessed at a repaired payload,
  dropped whatever it could not make sense of, and the turn reported success — so an unsupported
  key, a string where an array belonged, or a task with no runnable file scope became a plan
  quietly missing the work the planner had actually proposed. Execution readiness was never
  checked at planning time at all; a plan that could not run said nothing until `forge exec`
  answered `Execution blocked`. Four changes, none of them conditional on a repository, a task,
  or a model.
  (A) **The model gets to fix its own payload first.** A response that fails strict validation is
  re-sent with the exact validation errors, its own previous output, and an instruction that gets
  blunter each round — the polite ask, then strict mode, then a final attempt that says what
  happens next. Three attempts by default (`SYLLIPTOR_PLAN_REPAIR_ATTEMPTS`, capped at 6) at the
  existing JSON-retry temperature.
  (B) **Execution readiness is a repair trigger, not an exec-time surprise.** A payload that
  parses is simulated against a copy of the plan before it is accepted. If applying it would drop
  proposed tasks as unrunnable, or would introduce an acceptance-rule failure (`R1`-`R5`), the
  planner is re-prompted with that reason instead of a schema complaint it cannot act on.
  Failures the plan already had are excluded, so an inherited problem cannot burn the budget.
  (C) **Host repair is the last resort, and it is recorded.** Only after the attempts are spent
  does the host repair the payload itself. When it does, the dotted paths it had to change are
  written to the plan's `plan_repair` metadata and reported in `forge show` and the `plan_invalid`
  and `plan_saved` machine events, so a salvaged plan is never passed off as model-authored.
  (D) **Plans are saved with an explicit status.** Every save now stamps `plan_status` as
  `draft` or `execution_ready`, decided by the execution gate's own rule so the two cannot
  disagree, with the blocking reasons alongside it. `validate_plan` warnings are recorded but
  stay advisory. A planner that answers with questions and no plan for more than two rounds on the
  same goal (`SYLLIPTOR_PLAN_REPAIR_CLARIFICATION_ROUNDS`) is made to produce a concrete draft
  from the existing repo-grounding and greenfield-scaffold fallbacks rather than asking forever.
  `SYLLIPTOR_PLAN_REPAIR=off` reverts all of it.

- A model endpoint that stops returning usable responses no longer costs hours and then throws
  away the work. When a provider began answering with no text and no tool call, Sylliptor re-sent
  the identical request up to its attempt cap and terminated non-zero — observed as a run that
  spent 142 minutes after finishing 35 useful actions, and another that produced nothing for 40
  minutes, both ending on
  `model_control_error: The model repeatedly returned empty responses after tool results`. The
  attempt counter was the only limit: it never bounded wall-clock time, the retries re-sent the
  same possibly-poisoned context, and the terminal exit ignored a working tree that could already
  hold a complete change. Three changes, none of them conditional on a repository, a task, or a
  model.
  (A) **Stalls are detected on responses, not on branches.** Every main-model response is now
  classified where it arrives: a response carrying neither text nor a tool call is contentless and
  is counted; a tool-call-only response is ordinary work and is not. A stall is declared after
  three consecutive contentless responses (`empty_response_stall_threshold`) *or* after five
  minutes of an unbroken contentless streak (`empty_response_stall_seconds`), whichever comes
  first. The elapsed-time rule is the one the previous handler had no equivalent for: an endpoint
  taking minutes per empty call is now stopped on the second such response instead of running the
  attempt counter out over hours. Counting at the response site also means one authority decides,
  rather than each downstream branch seeing part of the picture.
  (B) **Recovery changes the input, and is bounded.** On a stall the turn spends one recovery
  cycle: the most recent tool-output block is compacted out of the context (roles and
  `tool_call_id` values preserved, so every assistant tool call keeps its matching result, and
  short results survive verbatim), the request is re-issued after a deadline-clamped exponential
  backoff, and the model is told plainly that the output was elided rather than being left to read
  a truncated result as complete. A session may spend at most two such cycles
  (`empty_response_max_recovery_cycles`) and at most ten minutes total on empty-response handling
  (`empty_response_handling_budget_seconds`); the existing targeted retry (forced tool choice) is
  unchanged and still runs first.
  (C) **Exhausted handling salvages instead of failing.** Tool writes land in the working tree as
  they happen, so a session that lost its model can still hold finished work. Instead of
  terminating, the turn now reports what is actually on disk — merging `git` diff state with the
  paths its own tools recorded, since neither source is complete alone — emits a local summary
  naming those paths, records a `session_degraded` event and a `degraded` marker on the final
  event, and exits 0 when material work persisted. The exit code is non-zero only when nothing was
  produced at all. `SYLLIPTOR_EMPTY_RESPONSE_STALL=off` reverts the whole behaviour, including the
  exit code.

- Verification-command selection is no longer able to abort a run. Selecting an authoritative
  verification command is an optimization over the work the agent is about to do, not a
  precondition for it, but an inferred command Sylliptor could not classify used to raise during
  session-prompt construction — before a single tool call — so the process exited non-zero having
  produced nothing. Observed on old-style repos with no obvious test config, where repo scan
  inferred `python -m doctest README.rst` and startup died with
  `authoritative verification command is invalid: ... unknown_verification_capability`. Three
  changes, none of them conditional on a repo, a task, or a benchmark.
  (A) **Doctest over documentation is never inferred.** Running the `>>>` examples in a README or a
  docs page asserts that the prose still renders; it asserts nothing about the code that was just
  changed, so it is not a verification surface. Repo scan no longer emits `python -m doctest <docs>`
  or pytest driven at doc files through `--doctest-glob`, and such commands are additionally rejected
  where inferred commands are promoted to an authoritative contract, so a scan persisted before this
  change cannot reintroduce one. Doctest that a *task* explicitly asks for is unaffected — that path
  is task-inferred and was never authoritative.
  (B) **An unclassifiable command degrades instead of raising.** The split is by *why* the command
  is invalid, not by who supplied it. A command Sylliptor could not classify
  (`unknown_verification_capability`, `unrecognized_command`) costs the session a verification
  contract and nothing more, so it degrades. A command Sylliptor recognized and *refused* — a
  vacuous verifier (`true`), a failure-masking chain, an unsafe pipeline, an unparseable command, a
  `curl` that cannot detect failure — still fails fast: running it would manufacture passing
  evidence, which is worse than stopping. Among the survivable ones, a command Sylliptor inferred
  itself is dropped and selection falls through a detection chain: the runner the workspace declares
  in `pytest.ini`, `[tool.pytest.ini_options]`, `tox.ini` `[pytest]`, `setup.cfg` `[tool:pytest]`, or
  `conftest.py`; then `unittest` discovery (mirroring the runner's own `test*.py` pattern, which
  catches layouts predating the pytest conventions the primary inference keys on); then any other
  runner repo scan already recognizes. Commands somebody *stated* — managed-host authoritative
  commands, `--verify-cmd`, configured `verify_commands` — are never silently rewritten: they are
  kept with a warning. The chain is consulted only after a selection turns out unusable, so the
  primary inference stays as conservative as it was: a repo that declares pytest but ships no tests
  still advertises no contract.
  (C) **No usable command is a best-effort session, not a failure.** When nothing is detected the
  session proceeds with an empty verification contract, emits a `verification_selection_degraded`
  warning, and records `verification_best_effort` in the session-start event. The exit code now
  reflects whether the agent ran and produced work, not whether command selection found something
  to run.

### Added

- `sylliptor run` now has a wall-clock budget that degrades in phases instead of running until
  something else stops it. Step limits bounded how *many* actions a run took but never how *long*
  it took, so a run that stopped converging kept going: across 361 benchmarked tasks the median
  successful run finished in 8.7 minutes and the 90th percentile in 26, no run produced a correct
  result after roughly 45 minutes, and failing runs continued for as long as 142. Time spent past
  that point was not work, it was a run that could not tell it had stopped making progress.
  A non-interactive run (`one_shot`, `forge_exec`, `swarm_worker`) now resolves a budget of
  **3600s (60 min)** by default, overridable with `--deadline-seconds N`,
  `SYLLIPTOR_RUN_DEADLINE_SECONDS`, or `run_deadline_seconds`, and removable with `--no-deadline`
  or any of the word forms `unlimited`/`never`/`off`/`none`. Selecting unlimited explicitly stops
  the search rather than falling through to the default, so the escape hatch actually works; `0`
  stays invalid, since a zero-second budget is exhausted before it starts. Interactive `sylliptor
  chat` is untouched — a person sitting at a session is their own timeout — and `--require-deadline`
  is deliberately excluded from the default so a managed host that asked to fail closed still does.
  The budget degrades in three stages rather than killing the run at the end:
  **(A) Convergence at 75% of the budget.** New subagents and new background processes are
  refused, and the model is told to drive what it already started to a verifiable state. Reads and
  edits stay available on purpose: convergence means "finish what you began", and a run cannot do
  that with its hands tied.
  **(B) Wrap-up at 90%.** File mutation and exploration are refused; verification, foreground shell
  commands, and the model call itself stay open, because "run final verification and write the
  summary" is exactly what this stage is for. `shell_run` was reclassified from `mutation_tool` to
  the existing `shell_tool` operation to make this hold — it is not a file mutation, and it is how
  most runs execute their tests, so leaving it misfiled would have closed the very verification
  this stage asks for.
  **(C) Expiry at 100%.** The run finalizes what exists and exits cleanly.
  Stages are keyed on elapsed *fraction* of the budget, not on the pre-existing finalization
  reserve, which makes the ladder monotonic: once a stage is reached the run never returns to a
  less degraded one. The finalization window's carve-out for writing required outputs can no longer
  reopen something wrap-up already closed, while a reserve window that opens *before* wrap-up
  (possible on short budgets) keeps that carve-out exactly as it was. Nothing degrades when the
  budget is unlimited, and `tool_dispatch` is never blocked, since blocking it would terminate a
  turn rather than degrade it.
  Expiry now shares the persisted-state guarantees of the empty-response salvage path rather than
  duplicating them: the same evidence rule (a git diff merged with the turn's recorded tool
  effects), the same `session_degraded` record, and the same exit-code rule — **exit 0 when
  material work was persisted**, non-zero only when nothing was produced. A run that ran out of
  time with a finished change on disk previously reported a bare failure and discarded it.
  Every stage transition is recorded in the session record as `deadline_phase_transition` with the
  elapsed fraction, remaining seconds, and the operations that stage closed; deadline telemetry
  carries the stage, the entered stages, and the policy. Thresholds are configurable
  (`run_deadline_convergence_fraction`, `run_deadline_wrap_up_fraction`; an inverted pair collapses
  to a single threshold rather than inverting the ladder), and
  `SYLLIPTOR_RUN_BUDGET_DEGRADATION=off` reverts to a plain budget with no phases.

- Forge commands now have a stable machine protocol. `sylliptor forge plan|show|status|review|exec|
  swarm|attach --machine` suppresses Rich output and writes newline-delimited JSON to stdout — one
  versioned envelope per line (`{"v":1,"event":...,"ts":...,"run_id":...,"data":{...}}`) with the
  events `run_started`, `plan_saved`, `plan_invalid`, `task_started`, `task_completed`,
  `task_failed`, `verification_result`, `verification_unavailable`, `review_result`,
  `run_completed`, and `error`. Integrators had no interface to bind to and were reduced to
  regexing stdout for phrases like `Plan saved`, which no test protected and any wording change
  broke. Human output remains the default and the interactive TUI is untouched.
  **Every invocation emits exactly one terminal event — `run_completed` or `error` — before the
  process exits**, on success, on handled failure, on an unhandled exception, on `typer.Exit`, and
  on Ctrl-C; the guarantee lives in a wrapper around each command body rather than in each error
  path remembering to fire, and events after the terminal one are dropped rather than appended.
  `forge plan` still reads planning input from stdin, with its prompt on stderr so stdout stays
  pure NDJSON. Swarm task transitions are reported from the single function every transition
  already passes through, so the lifecycle events cannot drift from the plan.

### Changed

- Forge exit codes no longer conflate "the work was rejected" with "the command failed". The
  contract is now `0` (the command did its job), `1` (it ran to completion but the work was not
  accepted), `2` (the command itself failed). **`sylliptor forge review` now exits 0 even when it
  rejects the work** — a review that requests changes is a review that succeeded, and the verdict
  is carried by `review_result.approved` (and by `Approved: no` in human output). Scripts that
  detected rejection via the exit code must read the approval flag instead. `forge swarm` now
  exits 2 rather than 1 on an unexpected exception, so exit 1 means only that the swarm ran and
  some tasks were not accepted; `forge exec` is unchanged, because a task that ran and was not
  accepted is a genuine execution failure.

- Server job status is derived from what the job reported rather than from what its exit code
  implies. Forge worker jobs run with `--machine`, and the runner reads the terminal NDJSON event
  out of the job output: `run_completed` with `ok:true` is `succeeded`, `ok:false` is `failed`, an
  `error` event is `failed` and carries its message. The exit code remains the fallback, and both
  `GET /v1/jobs/{job_id}` and `result.json` now report `status_source` (`terminal_event` or
  `exit_code`) plus the raw `terminal_event`, so a consumer can tell an observed outcome from an
  inferred one. `SYLLIPTOR_SERVER_MACHINE_EVENTS=0` restores human-readable Forge job logs and
  exit-code-only status; `run` jobs are never affected.

- VS Code production hardening now covers the complete extension-to-agent path: single-flight and
  stale-result-safe Chat/Forge startup, retained chat recovery, approval ownership, canonical
  lifecycle events, cooperative bounded bridge shutdown, inherited-credential stripping for
  diagnostic/no-secret launches, defensive UI/output redaction, real mock-provider bridge smoke,
  dual-version Extension Host coverage, and strict installable-VSIX content validation.

- IDE bridge reliability hardening: the stdio transport now isolates protocol stdout from incidental
  provider/library prints, reports worker-thread launch failures as redacted terminal jobs, bounds
  closed-session and terminal-job memory, and clears volatile replay/job state when a closed session
  id is reused. The VS Code client now performs single-flight startup/recovery and rejects stale
  session, job, Forge, artifact, and poll results after a user reset or bridge reconnect. Acceptance
  path parsing now models POSIX and Windows absolute paths independently of the host OS, preventing
  Windows from rewriting `/usr/...` paths onto the current drive or dropping drive-qualified
  workspace prefixes.

- Turn and session hygiene (verification step 5): three fixes for state an agent leaves behind
  after it declares itself done. Same idioms as steps 1–4 — pure decision functions, fact-based
  telemetry, per-feature kill-switch with env winning over config, no benchmark-conditional
  behavior, and system prompts byte-identical in every switch state.

  (A) **Tracked process-group lifecycle.** A timed-out test run used to leave its workers alive:
  `subprocess.run(timeout=...)` kills the direct child (the shell) and returns, and the pytest
  processes underneath it keep consuming the machine after the turn ends — observed twice on
  SWE-bench Verified subsets, where the orphans then split CPU with the authoritative verifier
  until it hit its own timeout. The shell runner now starts every command in its own POSIX process
  group and records the pgid while the group is alive; groups that outlive their tool call
  (timeouts, `&` backgrounding) stay tracked. At turn finalization an autonomous run (one-shot,
  Forge, swarm worker, subagent, or conflict resolver) terminates every tracked live
  group — SIGTERM, a bounded ~5s grace, then SIGKILL — emitting `process_reaped`
  `{pgid, command, origin, runtime_s, signal_used}` per group; this runs from a `finally` on the
  single turn boundary, so it covers success, honest-unverified, error, and cancellation alike.
  An **interactive** turn never auto-kills — a user may have asked for that dev server — and
  instead emits `process_survivors`; its groups are reaped at session close. The authoritative
  verifier's own commands are tracked too. Safety is structural: the registry only learns a pgid
  from the spawn that created it, nothing is ever matched by process name or cmdline, nothing is
  ever enumerated, and a pgid is refused outright if it is not a positive integer, is the agent's
  own process group, or — on Linux, where `/proc` makes it checkable — belongs to a leader whose
  start token changed (PID reuse). POSIX-only by a single platform guard; a true no-op on Windows,
  and the decision table stays unit-testable on any host. Kill-switch `process_reaping_enabled` /
  `SYLLIPTOR_PROCESS_REAPING=off`.

  (B) **Declared test-runner pre-provisioning.** Most sympy/sklearn/pylint sessions opened with the
  same wasted minutes: run the tests, watch them fail on a missing pytest, install it, run again.
  Workspace warmup now reads the runner the repo *declares* — `pytest.ini`, `[tool.pytest.ini_options]`
  in `pyproject.toml`, `[pytest]` in `tox.ini`, `[tool:pytest]` in `setup.cfg`, checked in pytest's
  own precedence order — and, when that runner is confirmed missing **and** the run is a top-level
  autonomous one, installs it once with `python -m pip install pytest --quiet`, logging
  `env_provisioned` `{package, trigger_config_file, success, ...}`. Both the importability probe and
  the install run **through the session's own shell path**, never through the agent's interpreter:
  that is the environment the agent's test command will actually resolve, and routing through it
  also keeps the operator's shell-sandbox policy binding on an action the agent would otherwise have
  taken itself. A session with the shell disabled (readonly) provisions nothing, and a probe that
  cannot answer — broken sandbox backend, interpreter not on PATH — is treated as unknown rather
  than as a missing package. Detection is config-file evidence only: a `tests/` directory or a
  `test_*.py` file says tests exist, not which runner was declared, and an indented `[pytest]` inside
  a multi-line INI value is a value, not a declaration. An importable runner is never upgraded or
  reinstalled, no other package is ever touched, the attempt is bounded to one per process (a failure
  is never retried, and a concurrent worker that loses the claim reports nothing rather than a
  phantom failure), and neither an **interactive** run nor a **nested** one (subagent or conflict
  resolver — either of which can be spawned from an interactive chat) ever installs:
  an interactive run emits `env_gap_detected` and leaves the call to the agent or the user. Malformed
  or oversized config degrades to "not declared"; a broken installer degrades to a warning rather
  than a failed startup. Kill-switch `workspace_provisioning_enabled` /
  `SYLLIPTOR_WORKSPACE_PROVISIONING=off`.

  (C) **Subagent fallback containment.** When a subagent exhausted its deadline, the runtime's
  locally generated stop report ("Completed work / Remaining work / Known issues", reconstructed
  from the child's own transcript) was handed to the parent as the delegation's result and reached
  the user's final artifacts as if it were deliverable work. That report is now marked *internal*
  where it is produced — the child's `final` event records `internal_fallback`, a recorded fact, not
  a phrase to look for — and the subagent boundary refuses to pass it up. The parent instead receives
  a structured incomplete status (`status: "incomplete"`, `error_code: "subagent_incomplete"`,
  `incomplete_reason`, `steps_used`, `deadline_s`, `report_artifact`) which it handles like any other
  failed delegation: retry, absorb the work, or report an honest failure. The report itself is
  persisted as an internal session artifact and recorded as `subagent_incomplete` telemetry, so
  nothing is lost for debugging. The same marker also stops the report reaching the user through the
  live surface — a nested session's assistant messages are forwarded to the parent's panel, so
  containing the dump only in the tool result would still have let the user watch it stream past.
  Exclusion from user-facing summaries is by construction: the summary builder consumes a transcript
  projection that drops marker-tagged messages, and the marker is stripped before any provider call.
  A top-level run is untouched — there the local stop report *is* the honest answer and is still
  shown — and a subagent that produced a real final report, including a model-written closing summary
  after an early stop, is unaffected.

## [0.9.7] - 2026-07-24

### Added

- Turn-contract v2 (verification step 4: apply-don't-advise + spec literalism). The completion
  gate now enforces that an execute-intent turn actually *applies* the change the task describes,
  at the literal it names — closing two failure modes seen on SWE-bench Verified: "described the
  fix in prose but made zero edits", and "shipped an alternative-mechanism fix that contradicts
  a literal the task shows". Built on steps 1–3 (route arbitration, evidence v2, regression
  baseline) without weakening any of them; no benchmark-conditional behavior, and the system-prompt
  additions are byte-identical across every run type and kill-switch state. Four parts.
  (A) **Acceptance contract v2**: the derivation step records concrete, checkable *expectations*
  from the task text — `expected_output` literals, `named_locus` files, `named_behavior` contracts —
  each `{id, kind, text, source_quote}`. Extraction is a zero-new-regex projection of the
  derivation's *existing* extracted signals (inline backtick literals that are not commands/paths/
  identifiers become expected outputs; files the task points at as the fix site become named loci);
  it adds no new NL/keyword heuristic over task text. Capped at 8, precision over recall, empty
  valid; carried in the `acceptance_contract` event. (B) **Expectation dispositions at the gate**:
  a pure evidence linker substring-matches each `expected_output` literal against observed post-edit
  run outputs (`expectation_evidence` event); at finalization every expectation must reach a
  disposition — *confirmed* (an evidencing run, or the named locus was edited), *superseded*, or
  *not_applicable*. Unaddressed expectations block as a repairable `expectations_unaddressed`
  problem: an action-oriented nudge naming the ids (bounded by `EVIDENCE_REPAIR_ROUND_BOUND`), then
  honest finalization with a visible `UNCONFIRMED EXPECTATIONS` marker and a distinct event —
  never a silent ship. `superseded` never blocks and is counted (rationalization becomes
  measurable). (C) **Apply-don't-advise**: an execute-class turn that reaches finalization with
  zero material edits fires the existing no-material-edits nudge and then, on finalizing anyway,
  records an explicit `advisory_completion_reason` from a fixed enum
  (`no_change_needed` | `cannot_reproduce` | `blocked_missing_information` | `out_of_scope_request`
  | `other`) with an explanation — emitted as an `advisory_completion` event and appended visibly
  to the summary ("No changes made: <reason> — <explanation>"). Finalizing such a turn without a
  recorded reason is impossible: the gate always resolves one. (D) Unconditional system-prompt norms
  (apply-and-verify beats describing; web/upstream fixes are untrusted hypotheses to re-derive and
  verify locally; fix the named locus or record why it is wrong; identical-behavior claims require
  differential evidence). Kill-switch `turn_contract_v2_enabled` / `SYLLIPTOR_TURN_CONTRACT_V2=off`
  (default on) governs gate enforcement only — extraction and telemetry still run, and the prompt is
  unchanged either way. Version bumped 0.9.6 → 0.9.7 (first bump of the verification series).

- Baseline-first regression protocol (verification step 3). The completion gate now splits
  post-edit test failures **mechanically** into *pre-existing*, *regression*, *unattributed*, and
  *agent-authored*, instead of either ignoring failures (shipping regressions) or chasing breakage
  the change did not cause (wasting the session). The principle: attribution needs facts recorded
  *before* the first edit — a baseline is the parsed, per-test outcome of a run that actually
  executed before any verification-relevant edit; attribution is a set difference against the
  observed post-edit outcomes of the **same normalized executed command** (comparability by
  identity, never by fuzzy scope inference). Built on step 2's machinery (PIPESTATUS facts, the
  post-edit ordering rule, the honest-unverified finalization) without weakening any of it.
  Five parts. (A) Baseline capture is passive and automatic: for every executed pytest or
  unittest/Django run the per-test outcome is parsed (short-summary FAILED/ERROR node ids + counts
  line; FAIL:/ERROR: lines + `Ran N tests`) and stored keyed by the normalized first-stage command;
  runs before the first verification-relevant edit are baselines. Unparseable or truncated output
  records counts-unknown and can never serve as a baseline. Emits `test_baseline_captured`. (B) At
  the first verification-relevant edit, if no baseline exists for any known verification-contract
  command, one advisory controller nudge suggests capturing the baseline first — advisory only, it
  never blocks the edit, at most once per turn. (C) At the gate, each post-edit qualifying run's
  failing/errored ids are classified: *pre_existing* (in the same-command baseline), *regression*
  (absent from it), *unattributed* (no comparable baseline — a distinct honest state, never treated
  as pre-existing OR regression), and *agent_authored* (a test file the agent created this turn — a
  failing repro is signal, not a regression). Results go to the completion certificate and a
  `regression_diff` event. (D) Gate policy: regressions block finalization exactly like a step-2
  evidence deficit — action-only (prose cannot clear it), the repair names the regressed ids and
  the same-command baseline that proves them new, bounded by the same 2 repair rounds, then honest
  finalization with a visible `REGRESSIONS UNRESOLVED: <ids>` marker and a distinct
  `one_shot_completion_gate_regressions_unresolved` event; only-pre-existing failures do **not**
  block and are listed as `pre_existing_failures` so the summary can state them (the sympy-12489
  behaviour, mechanized); unattributed failures get one attribution round, then finalize honestly
  with an `UNATTRIBUTED FAILURES: <ids>` marker; pure-advisory / no-edit turns are entirely exempt.
  A named contract command failing always blocks — attribution never clears it (step 2 intact).
  (E) Same logic for benchmark and normal runs (no harness/env detection); prompts are byte-
  identical with the feature on or off. Parsers, the differ, and the gate-policy decision are pure
  functions; on ambiguity a parser returns counts-unknown rather than guessing, and never raises. No
  git state is ever mutated to reconstruct a baseline. Kill-switch: `regression_baseline_enabled`
  config key or `SYLLIPTOR_REGRESSION_BASELINE=off` (default on) — capture still records telemetry,
  the gate policy reverts to legacy.
- Fact-based completion-evidence gate (evidence v2). The completion gate now judges *observed
  execution facts* — which program ran, its per-stage exit status, and when it ran relative to the
  last mutating edit — instead of regexing the command string for pipes or control flow. Three
  principles: (1) evidence is facts, not string patterns; (2) the gate can never be satisfied by
  prose — only a new execution event clears an evidence deficit; (3) fail honest — if evidence
  can't be obtained after bounded repair, the run finalizes with an explicit UNVERIFIED status
  rather than silently accepting. Four parts: (A) the shell tool records per-stage pipeline exit
  codes (bash `PIPESTATUS`) alongside the overall exit code, in the tool-result metadata and
  session event; `pipefail` is never enabled and stdout/stderr/exit-code are byte-preserved, so a
  `pytest … | tail -40` whose real failure the trailing `tail` masked is now visible. (B) The
  evidence classifier consumes those facts: a pipeline's meaningful program is its first stage, and
  `PIPESTATUS[0]` is the ground truth — piped-pass counts as passing evidence, piped-fail as
  failing evidence; when per-stage status is unobservable it falls back to a single bounded unpiped
  re-execution of the first stage, and otherwise the pipeline is treated as unverified (never
  silently accepted). The `unsafe_pipeline`/`disallowed_shell_control_flow` string heuristics no
  longer gate observed pipeline evidence; the separate existing-test-edits finalization guard is
  unchanged. (C) Ordering rule: finalizing an execute-posture turn that made mutating edits to a
  verifiable surface requires a qualifying execution-evidence event *after* the last such edit;
  syntax-only checks (`ast.parse`, `py_compile`, linters) do not qualify. Turns with no mutating
  edits (Q&A/analysis/advisory) and workspaces with no verification surface are exempt. (D) The
  repair nudge names the missing fact concretely ("no test execution after your edit to X at step
  N — run the relevant tests now"); a prose-only reply can no longer clear the deficit. Repair is
  bounded (2 rounds); on exhaustion the run finalizes honestly-unverified with a distinct
  `one_shot_completion_gate_unverified_finalized` telemetry event, a `honest_unverified` flag on
  `completion_gate_accepted_with_open_problems`, and a visible UNVERIFIED marker in the final
  summary. Evidence decisions log the command, per-stage statuses, and post-edit ordering. Same
  logic for benchmark and normal runs (no harness/env detection); prompts are byte-identical with
  the feature on or off. Kill-switch: `evidence_v2_enabled` config key or `SYLLIPTOR_EVIDENCE_V2=off`
  (default on) reverts to the legacy string-shape classifier.

- Route arbitration: a deterministic capability-provisioning layer over the router LLM's verdict,
  built on the principle that routing decides which capabilities are provisioned, not what the
  agent must do — an escalated turn may still legitimately answer in prose ("tools present but
  unused" is free; "tools wrongly absent" is unrecoverable). Two rules, both operating only on
  existing classifier outputs and runtime facts (never on raw message text):
  (1) `one_shot_provisioning` — non-interactive runs bound to a writable repo-backed workspace
  always keep the repo toolset provisioned; a `general`/`tool`/`chat` router verdict can no
  longer strip execution capability and produce a zero-step tech-support answer to a pasted
  defect report. (2) `classifier_disagreement` — interactive runs escalate a non-execute router
  verdict (route `general`/`tool`, or `repo` with an advisory/plan posture) to repo/execute when
  the turn-intent classifier says the turn is execute-class; agreement on advisory/plan is
  respected unchanged, which preserves normal Q&A, and `chat` verdicts plus fallback-sourced
  decisions are never escalated. Every `route_decision` event now records
  `router_intent_execution_disagreement` (raw router-vs-intent error rate, logged even when no
  override fires; `null` on fallback-sourced turns where no router verdict exists), and each
  override emits a `route_arbitration_override` event with the pre-arbitration verdict, signals
  used, and rule id. Kill-switch: `route_arbitration_enabled` config key or
  `SYLLIPTOR_ROUTE_ARBITRATION=off` (default on); the router prompt is untouched.

- Redesigned the `/config` TUI: the top-level menu is grouped into Workspace / Model / Behavior
  sections with one short fact per row (full detail lives inside each section), section headers and
  breathing room, no digit-number noise, a single key legend in the footer, warn-tinted problem
  summaries (e.g. a missing required model), and cursor memory — Esc returns to the row you came
  from. Provider preset and model pickers show one line per option instead of wrapped protocol
  paragraphs, and empty model lists now explain themselves instead of rendering a blank screen.
- Refreshed the entire provider model catalog against a July 2026 audit of official provider docs
  and coding-agent registries: 58 model ids added, 25 removed, deadline-critical aliases in place
  before provider shutdowns (DeepSeek legacy ids, Mistral devstral, Groq llama, xAI retired slugs),
  a `validation_model` on every preset, the unmaintained 01.AI preset removed, and honest setup
  warnings on Perplexity (search-only), ByteDance (probe-gated ids), and the OpenAI chat preset
  (tools + default reasoning effort is rejected — use the Responses preset for agentic runs).
- Reworked Advanced per-role model overrides from a forced walk through every role into role
  lists: pick a role, edit just that role (blank keeps, "clear" inherits — now supported for
  subagent overrides too), and land back on the list with the change visible.
- Added a static per-model reasoning contract table (`reasoning_contracts`): mode
  (always-on/optional/none/unknown), wire spelling, allowed effort values, and off-path semantics
  per provider surface. The `/config` thinking picker now consults it — invalid efforts are hidden,
  models that cannot disable reasoning say so, and Kimi Code's "off" warns that the request is
  silently routed to K2.6 (a different model).
- Added first-class Moonshot/Kimi runtime support for K3, K2.7 Code,
  K2.7 Code Highspeed, and K2.6, including provider-scoped context, output,
  vision, reasoning, and global pricing metadata. Kimi requests now preserve
  reasoning continuation state, apply each model family's supported thinking
  and tool-choice shape, support resume-stable session cache affinity in
  automatic cache mode, and normalize Moonshot's cached-token usage for cost reporting.

### Fixed

- Saving /config before sending the first message no longer exits and restarts the whole TUI
  (with its slow cold boot): the pending session simply picks up the freshly saved config, and the
  footer updates in place.
- Fresh sessions on shared-window models (e.g. Kimi Code) no longer show "context: 0% left":
  metadata declaring `max_output == context` collapsed the input budget to a 512-token floor and
  triggered absurdly early compaction; the registry now reserves a conservative output fraction.
- Config input screens no longer print a second, sometimes contradictory key hint; the footer shows
  each screen's real action verb ("Enter save", "Enter add"). The Context & Cache header shows a
  plain-language caching status instead of a raw policy debug dump, and the API-key screen shows
  "saved in profile X" instead of internal source strings.

- Added a startup update prompt: when a newer PyPI release is known from the
  cached update check, interactive launches (`sylliptor`, `sylliptor chat`)
  now show a popup before the first-run setup and workspace screens asking
  whether to update now, be reminded later, or skip that release. "Update now"
  runs the detected installer command (pipx / uv / pip) and asks you to
  restart; "later" snoozes the prompt for 24 hours; "skip" silences that
  version permanently. Editable/source checkouts are never prompted, and the
  decision is cache-only so launches never wait on the network. Disable with
  `sylliptor config set update_prompt_enabled false` or
  `SYLLIPTOR_UPDATE_PROMPT_ENABLED=0`.
- Scoped session listings to their creator: every session log is now stamped
  with a local owner identity (`os-user@hostname`), and `/resume`,
  `sylliptor sessions list`, and the "latest session" defaults of
  `sylliptor sessions score`, `sylliptor feedback`, and the hook audit
  commands never surface a session recorded by a different account, no matter
  how the log file arrived in the sessions directory (copied archive, baked
  disk image, shared host). Legacy logs without an owner stamp stay visible so
  upgrades never empty a user's own resume list. Foreign-stamped sessions
  remain reachable deliberately: explicit `/resume <id>` works even when the
  scoped picker is empty, and `sessions list --all` includes hidden sessions
  with a hidden-count hint. If a user's identity drifts (e.g. a hostname
  rename), one explicit resume re-stamps the log's tail and the session
  self-heals back into their listings.
- Added a keyless `ddgs` external web-search adapter (DuckDuckGo-family
  metasearch) as the always-ready last-resort backend, so `web_search` works out
  of the box for every provider and model — including DeepSeek, Together,
  Fireworks, and local endpoints — with only the chat API key configured. The
  auto-mode call-time fallback chain is now provider-hosted → Tavily → ddgs;
  opt out with `SYLLIPTOR_WEB_SEARCH_KEYLESS=0`.
- Made web-tool failures recoverable when the model can fix them: invalid
  `web_search` arguments and structured `web_fetch` provenance rejections are
  returned to the model as plain errors for a corrected retry, instead of
  disabling web tools for the rest of the turn. Only unrecoverable
  backend/connectivity failures after fallback exhaustion still degrade the turn.
- Added model-led, model-independent web search with provider-hosted adapters for
  OpenAI, Anthropic, Gemini, OpenRouter, Qwen, Zhipu, Kimi, MiniMax Token Plan,
  Doubao, Groq, Mistral, xAI, Cohere v1, and Perplexity, plus shared external
  search for DeepSeek, custom models, and providers without hosted search APIs.
- Simplified `web_search_policy` to an `auto`/`off` capability switch. The model
  now decides when external evidence is needed through its normal tool loop;
  legacy `always` configuration is normalized to `auto`.
### Fixed

- Fixed non-repo tool turns discarding all gathered evidence when the tool-step
  budget ran out: the turn now finalizes with one more model call over the full
  tool transcript (tools disabled) instead of degrading to the no-context
  clarification fallback that could only ask "Can you provide more details?".
- Treated URL-specific `web_fetch` failures as recoverable — HTTP status errors
  (403/404/429/…), unusable payloads, oversized bodies, redirect problems, and
  blocked or invalid hosts: the failure is specific to that target, so the model
  keeps web tools and can try a different source. Only genuine connectivity
  failures (DNS outage, connect errors, timeouts) still disable web tools for
  the turn, and the unavailable observation no longer tells non-repo chat turns
  to "proceed using the repository".
- The TUI tool trace now shows the argument preview (search query, fetched URL,
  file path) in the live status and completed "✓" lines, and consecutive runs
  of the same tool group as "↳" continuation rows under one header instead of
  repeating the tool name. Shell command lines are never previewed (they can
  carry secrets).
- Stamped every `web_search` result with `retrieved_at` — the UTC wall-clock
  time the search executed — so models can anchor "today"/"current" reasoning
  to real time instead of their training prior. "What date is today" now
  resolves from a single search instead of failing after four.
- Replaced the deterministic macOS/Linux OAuth credential fallback with an
  atomically created per-store random master key, added transparent migration
  for legacy encrypted stores, and prevented low-level credential diagnostics
  from corrupting the full-screen TUI through raw stderr.
- Wired the Terminal-Bench adapter into the managed-host deadline contract so
  benchmark runs now pass `--deadline-seconds`, `--require-deadline`, and a
  positional `--` before task instructions, with fail-closed host-timeout
  validation and sanitized deadline diagnostics.
- Removed the Terminal-Bench fake `true` verifier default and made authoritative
  verification reject vacuous, non-assertive, and failure-masking commands before
  they can satisfy completion gates.
- Consolidated verification command parsing behind a canonical analyzer, restored
  safe `cd <workspace> && python -m pytest ...` evidence, rejected unsafe
  pipelines by default, derived `ok`/`all_passed` from explicit command status,
  fingerprinted pre-existing checker entrypoints, and hardened managed-host
  verifier validation and unavailable behavior.

## [0.9.6] - 2026-06-16

### Fixed

- Updated the sandbox Go toolchain to 1.26.4 so dev and server image
  vulnerability scans use the fixed Go standard library.

## [0.9.5] - 2026-06-16

### Fixed

- Refreshed the sandbox image base digest so release image vulnerability scans use the
  current Python 3.12 slim base.

## [0.9.4] - 2026-06-16

### Added

- Polished Forge CLI planning and execution feedback with clearer readiness, next-step,
  completion, and swarm trace status lines.

### Fixed

- Kept the welcome banner side-by-side at 80 columns when dark terminal themes add the
  light owl panel.
- Restored the Forge asset count line for empty plans that already have indexed assets.

## [0.9.3] - 2026-06-12

### Added

- Hosted trial model choice: `mimo-v2.5-pro` is now the default, with `mimo-v2-flash`
  and `mimo-v2.5` available in `/config` and `/model`; live `/v1/models` discovery,
  login model preservation, and legacy `mimo` alias migration keep existing installs
  working while exposing the full trial model set.
- Xiaomi MiMo logo in the README trial section.

### Fixed

- Removed the duplicate Xiaomi trial section from the README.

## [0.9.2] - 2026-06-11

### Added

- Hosted **Xiaomi MiMo** free-trial provider: `alysis login` / `logout` / `whoami` connect the CLI to your Alysis Code account over a localhost browser handshake and unlock the 10-day MiMo trial — no API key, usage metered server-side, and the upstream model key never exposed to the client.
- `alysis` provider preset (selectable in setup) routing to the hosted MiMo proxy with `mimo` as the default model, plus a one-step "connect now" offer at the end of setup and `/login` `/logout` chat commands.
- `mimo` built-in model metadata (262144 context / 16384 output) so the trial model uses its full context window instead of the 8192/2048 fallback.
- Friendly CLI messages for trial-state proxy errors (trial expired, quota exhausted, rate limited, …) in both interactive chat and one-shot `run`.

### Fixed

- Force UTF-8 stdout/stderr at startup so the rich UI no longer crashes on non-UTF-8 consoles (e.g. Windows Greek cp1253).

## [0.9.1] - 2026-06-08

### Added

- Native API protocols for OpenAI, Anthropic, and Gemini provider profiles.
- Provider-native and external web search backends, including Tavily when configured.
- Router model selection in setup and configuration so lightweight routing can use a cheaper model.

### Fixed

- Hardened provider diagnostics, Forge/runtime status reporting, asset display, and cross-platform test behavior.
- Improved configuration, profile, MCP, hook, and tool output handling for clearer public CLI behavior.

## [0.9.0b2] - 2026-05-09

### Fixed

- Restored the sandbox image workflow for GHCR publishing and runtime smoke validation.
- Fixed sandbox image smoke checks by avoiding nested container init handling.
- Updated release metadata so the next beta can publish as a new PyPI version.

### Changed

- Cleaned stale beta support wording and public issue template version examples.

## [0.9.0b1] - 2026-05-09

### Added

- First beta package published to PyPI as `alysis-agent-cli`.
- GitHub Actions release workflow with build, test, package smoke, and PyPI Trusted Publisher publishing.
- Public governance files, contribution templates, funding metadata, and polished docs navigation.

### Changed

- Public-facing repository documentation was trimmed and polished for beta launch.
- README and docs navigation now point visitors to the public Alysis Code website.

## [0.1.4] - 2026-04-06

### Added

- Mutable chat session workdirs for changing task context during a session.
- Live surface feedback, including a thinking spinner and streamed Markdown rerendering.

### Fixed

- Plan Mode entry flow, exact plan subcommand parsing, and natural-language workdir navigation.
- Review approval keyboard navigation.

## [0.1.3] - 2026-04-02

### Added

- Production-grade Skills validation flow and installed CLI smoke coverage.
- Skills lifecycle guidance for first-time authoring and installation flows.

### Fixed

- Invalid `/plan` guidance and blank MCP prompt server filter handling.

## [0.1.2] - 2026-03-31

### Added

- First Skills MVP with authoring, lifecycle, validation, and evaluation support.
- MCP foundations for stdio and streamable HTTP, including tools, roots, resources, and prompts.
- Tier 1 custom tools foundation and bundled model catalog provenance.

### Changed

- Chat Plan Mode, repo grounding, and approval flows were tightened for clearer read-only planning.
- Forge and swarm execution were hardened around task scope, verification, and planner handoff.

### Fixed

- MCP transport completion edge cases, protected follow-up synthesis, and atomic runtime artifact handling.

## [0.1.1] - 2026-03-18

### Added

- Core local agent CLI with chat and run modes, model provider API access, tool execution, streaming responses, image inputs, clipboard support, and slash commands.
- Setup, configuration, usage tracking, conversation compaction, history search, and workspace binding flows.
- Forge planning and execution workflows with swarm orchestration, worktrees, verification gates, review gates, conflict review, and feedback bundle export.
- Sandboxed shell execution, isolated server worker jobs, web fetch/search tools, subagents, and the first extensions foundation.

### Changed

- Rebranded the package and visible CLI surfaces from the initial Coder naming to Alysis Code and Forge.
- Hardened runtime safety across git operations, protected paths, shell policy, workspace scope, sandboxing, and verification evidence.

### Fixed

- Cross-platform verification, terminal UX, task routing, prompt handling, and recovery paths across managed execution flows.

## [0.1.0] - 2026-02-13

### Added

- Initial Python package scaffold for a local coding agent CLI.
- Baseline README, architecture notes, Apache-2.0 license, packaging metadata, and development configuration.
