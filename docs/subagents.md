# Subagents

## Subagents (Non-Swarm)

Subagents are optional focused helper agents that run as nested sessions from normal
`alysis run` / `alysis chat` (non-swarm) flows. Their model context, messages, and tool loop
are isolated from the parent transcript. The default `workspace_view=shared` uses the parent's
working tree; `workspace_view=isolated` uses a retained Git worktree.

Forge execution and swarm workers currently do not expose subagents. Use subagents during
top-level exploration or planning, then execute scoped Forge tasks directly.

### Current orchestration capabilities

The parent can run one child synchronously or keep working while background children move through
queued, waiting, running, and terminal states. Independent shared-readonly and isolated calls can
run in parallel; isolated writers leave retained candidates for explicit apply or discard, and a
read-only child can verify the exact pinned candidate first. Parents can steer live children,
chain dependent runs, inspect or resume terminal work, and delegate external dependency research
to the evidence-gated `dependency-scout`. Write-capable children may use a bounded set of
non-editing helpers without opening recursive delegation.

Default behavior is ON for top-level chat/run sessions. Use `--no-subagents` or
`alysis config set subagents_enabled false` to disable it.

Enable/disable options:

- config: `alysis config set subagents_enabled true|false`
- per command: `alysis run --subagents ...` / `alysis run --no-subagents ...`
- per command: `alysis chat --subagents` / `alysis chat --no-subagents`

UX behavior in chat:

- Toolbar includes `subagents=on|off` so the current state is always visible.
- Users can describe the outcome in normal chat; they do not need to select a subagent or name an internal tool. The repository agent loop receives a capability catalog and the model chooses whether an available specialist is useful.
- `/subagents` is the only subagent chat command. It opens a picker containing currently active
  children, shows each child's state and activity, and selects one into the existing live pane.
  When none are active it prints `no active subagents`. The command is read-only and works while
  the parent turn is running.
- In the TUI each subagent is identified by its name plus an activity tagline (e.g.
  `debugger · hunting the root cause`) in the spawn line, live status, and result attribution.
  Agents wear no per-agent symbol. Custom subagents get a stable name-derived accent colour and
  fall back to their description.
- While subagents run, the TUI footer shows one count badge such as `↪ 3 subagents` rather than
  one badge per child. It clears when the final child ends or the parent is interrupted. Elapsed
  activity time is measured from each child's own start, independently of the parent session's age.
- With trace set to `compact` or `full`, subagent runs stream nested live tool progress under the parent session instead of staying silent until completion.

Request parallel independent work through normal chat, for example:

```text
Use three read-only subagents in one parallel batch: map the architecture, inspect the tests,
and review concurrency risks. Wait for all three, verify their key claims, and synthesize one
answer. Do not edit files.
```

The calls must be emitted together in one assistant response. The host runs at most four eligible
children concurrently and returns results in call order, even when children finish out of order.
Eligibility is based on workspace safety, not merely whether the child can run commands:

| Workspace view | Role contract | Same-response scheduling |
| --- | --- | --- |
| isolated | any effective mode | parallel |
| shared | effective mode is exact `readonly` | parallel |
| shared | `allow_workspace_writes: false` and `parallel_nonwriting_shared=true` | parallel |
| shared | write-capable, or non-writing without the opt-in | sequential |

Eligible calls may be mixed in one parallel batch. An ineligible call no longer serializes its
siblings: if at least two calls are eligible, that subset runs first (up to four at once), then the
remainder runs one at a time. With fewer than two eligible calls, the batch stays fully sequential.
Mixed subagent/non-subagent responses, user opt-out, tool hooks, and deadline admission retain their
all-or-nothing safety gates. Whenever two or more requested subagents are not fully concurrent, the
surface prints one line naming the deferred roles and reason, and the session records a
`subagent_batch_serialized` event.

Shared non-writing command-capable roles such as `debugger` and `verifier` are default-off for
parallel execution because a shell command can still mutate the shared tree. Enable the explicit
opt-in with:

```text
alysis config set subagent_orchestration.parallel_nonwriting_shared true
```

The host still checks material workspace changes after each non-writing run. If an opted-in shared
child mutates the tree, the entire batch fails, names the offending run and paths, and does not start
the deferred remainder. Write-capable shared roles remain sequential; use `workspace_view=isolated`
to run them concurrently. Every isolated writer receives a separate worktree.

### Background children

Top-level chat and one-shot sessions can start independent work and continue while it runs:

- `subagent_spawn` starts a child and immediately returns its `run_id` and state.
- `subagent_send` queues parent guidance for delivery before the child's next model step.
- `subagent_status` reports one child or all children without joining them.
- `subagent_wait` joins one child or all children. An optional timeout returns the completed
  subset plus `pending_run_ids`, so the parent can continue and wait again later.
- `subagent_cancel` cancels one child or all children. A queued child is cancelled without ever
  launching a nested session. Its result separates runs that actually transitioned under
  `cancelled_run_ids` from `already_finished_run_ids` and `unknown_run_ids`.
- `subagent_resume` relaunches a failed, incomplete, or cancelled terminal child as a new linked
  background run.

Use background spawning only when the investigation is independent of the parent's next decision.
Use synchronous `subagent_run` when the result is needed immediately; its input, output, event,
sanitization, deadline, and usage contracts are unchanged.

Synchronous and background children share one parent-session run-ID namespace. Status, live-pane
inspection, and resume resolve either kind identically, including a retained synchronous run that stopped
incomplete. This registry unification is bookkeeping-only: existing lifecycle events retain their
order and payloads.

An immediate child follows `spawned -> queued (if full) -> running -> joined|cancelled`. A chained
child follows `spawned -> waiting -> queued -> running -> joined|cancelled`. The background cap
defaults to 3, queue release is FIFO, and the same-response batch cap remains 4.

Shared-view background children must resolve to exact `readonly`. An isolated background child may
use any post-clamp mode because it owns a separate worktree. Children cannot spawn nested children,
and Forge execution and swarm workers do not receive the background tools.

A parent turn never completes with an unjoined child. When the model tries to finalize early, the
host sends at most two corrective messages asking it to wait or cancel. If it still finalizes,
`subagent_orchestration.turn_end_policy` decides the outcome:

- `wait` (default): the host joins every child, injects the collected results, and allows one final
  model response.
- `cancel`: the host cancels every child and appends a cancellation notice to the final response.

User interruption cancels running and queued background children. Usage records are replayed
incrementally while a child runs and once more at join, with a per-child cursor preventing double
counting in `/usage` and the HUD.

Joining an isolated child does not discard its candidate. Unapplied results are retained across
turns, named in the injected results/final notice and the next `<subagent_turn_context>`, and remain
available to apply or discard. The terminal `final` event keeps the complete assistant report and
appends this notice, so event-only consumers do not lose the report. Closing the parent session
releases every remaining worktree.

Resumption restores the original persisted conversation, synthesizes missing tool results when a
turn ended mid-call, and re-evaluates the role against the current parent mode, sandbox, deadline,
and step budget. The new result and lifecycle events carry `resumed_from=<old_run_id>`. By default,
an unreleased isolated candidate is transferred to the new run at the same worktree path, so only
the new run can later be applied or discarded. Pass `reattach_workspace=false` to start from the
role's normal workspace view instead; the old patch artifact is still named in the resume context.
Active, successful, degraded, unknown, and already-released isolated runs return structured errors.
An incomplete result and its `subagent_end` event name the actual `stop_reason`, steps used and
resolved ceiling, deadline remaining, retained run ID, and an exact `subagent_resume(run_id=...)`
affordance when resumption is possible. Resume or steer retained work instead of rebuilding it.
Resume also carries forward content-hashed read-ledger entries that still match the attached
worktree, so unchanged ranges are not returned again; changed files are read normally.

### Steering background children

Use `subagent_send(run_id, message)` to correct or refine queued, waiting, or running work without
restarting it. The child receives `Message from the parent agent: <text>` as a system message before
its next model call; queued and waiting children receive stored messages at their first step. The
limit is 4,000 characters. Unknown or terminal runs return structured errors, and successful sends
emit additive `subagent_message` telemetry.

### Chained background children

`subagent_spawn(..., depends_on=[run_id, ...])` leaves a child in `waiting` until every dependency
joins successfully. A failed, degraded, incomplete, or cancelled prerequisite cancels the child
with `dependency_failed`; the child never launches. Duplicate, unknown, self-referential, and cyclic
dependencies are rejected. Waiting children remain part of turn-end wait/cancel enforcement, and
their deadline eligibility is checked again when they become runnable.

The one-response candidate flow is: spawn an isolated implementer with `run_id=impl`, then spawn a
verifier with `depends_on=[impl]` and `workspace_from_run=impl`; wait for both and call
`subagent_apply(impl)` only after the verifier passes. Caller-selected IDs accept only 1-64 ASCII
letters, digits, underscores, or hyphens and must be unique in the parent session. Pinned-worktree
resolution is deferred until the implementer joins and captures its candidate.

Each completed same-response parallel batch and dependency chain emits one additive
`subagent_batch_summary` event with ordered run IDs, statuses, wall time, summed usage, and workspace
views. It does not change model-facing results.

Configuration examples:

```text
alysis config set subagent_orchestration.max_background_children 3
alysis config set subagent_orchestration.turn_end_policy wait
alysis config set subagent_orchestration.parallel_nonwriting_shared false
```

### Isolated workspaces

Pass `workspace_view=isolated` to `subagent_run` when a child should work away from the parent's
working tree. Alysis Code creates a detached Git worktree at the parent's current `HEAD` under the
session artifact directory: `subagent_worktrees/<run_id>`. The child keeps its normal mode,
allow/deny write scopes, and workspace-mutation reconciliation; those policies are evaluated
inside the worktree.

An isolated child cannot see uncommitted parent edits because its base is `HEAD`. The prepare event
and returned `workspace` metadata therefore include `parent_dirty_paths`, the paths hidden from the
child at launch. The result also contains a patch summary (files, insertions, deletions, SHA-256,
and an internal artifact locator). The complete unified patch is persisted as a session artifact
and is never placed in the model-facing tool result.

After reviewing the result, use:

- `subagent_apply(run_id)` to check and apply the captured patch to the parent working tree.
  Application is serialized, uses `git apply --check` first, and leaves successful changes
  uncommitted. A conflict applies nothing and retains the worktree for inspection. Applying a
  candidate whose run did not succeed requires `acknowledge_incomplete=true`; otherwise the tool
  returns its status, stop reason, and unfinished-work summary without changing the parent tree.
- `subagent_discard(run_id)` to delete the worktree without changing the parent.

Worktrees remain available until applied, discarded, or the parent session closes. Repeated,
unknown, already-released, and empty-patch actions return structured errors. Version 1 supports
Git repositories only; a plain directory returns
`workspace_view=isolated requires a git repository`. Disable the feature with:

```text
alysis config set subagent_orchestration.workspace_isolation_enabled false
```

The workspace provider never creates commits, and applying a result never commits the parent tree.

### Parallel writers and pinned verification

The recommended isolated implementation flow is:

1. Run or spawn an `implementer` with `workspace_view=isolated`.
2. Run or spawn a non-writing verifier with `workspace_from_run=<implementer_run_id>`.
3. Call `subagent_apply(<implementer_run_id>)` only after verification passes; otherwise discard it.

`workspace_from_run` mounts the completed, unreleased candidate worktree as the verifier's root, so
the verifier sees the candidate while the parent tree remains unchanged. It is available to roles
whose definition disallows workspace writes, including `verifier`, `explorer`, `code-reviewer`, and
`debugger`. The source worktree is release-locked until that child finishes; apply/discard returns
`workspace_release_locked` while pinned. Unknown, unfinished, and released source IDs return
structured errors. A verifier does not create a second candidate worktree.

### Helpers inside write-capable children

Depth-1 write-capable children may consult a small non-editing helper roster without returning to
the parent between implementation steps. Built-in helpers are `explorer` for repository research,
`code-reviewer` for independent review, `verifier` for evidence-based checks, and `debugger` for
root-cause investigation. Automatically routable custom roles with
`allow_workspace_writes: false` are also eligible. Readonly children, Forge
execution, and swarm workers do not receive this tool.

The helper runs synchronously in the same root the writer sees. An isolated writer's helper reads
that writer's worktree; a shared writer's helper reads the shared root. Helper reports are advisory
and never replace the writer's own verification duty. Helpers cannot spawn, wait for, apply, or
discard children, and depth-2 sessions expose no `subagent_run`, so nesting stops absolutely there.

Defaults are two helper calls per writer, 20 steps per helper, and 120 seconds per helper. The
effective deadline is the earlier of that timeout and the writer's remaining deadline. Calls are
sequential, and an exhausted budget returns a structured `helper_budget_exhausted` result. Helper
launch also requires enough remaining time for twice the writer's current robust per-call latency
estimate. A helper that cannot plausibly complete two model calls is refused before launch so the
writer can continue directly. Deadline-blocked child events make the terminal
`subagent_end.deadline_exhausted` summary true.
model usage first replays into the writer; the parent's normal writer replay then carries both
writer and helper records once. The writer result and `subagent_end` event summarize `count`,
`names`, `steps`, and `usage_totals` under `helper_runs`.

```text
alysis config set subagent_orchestration.helpers_enabled true
alysis config set subagent_orchestration.helper_max_total_per_child 2
alysis config set subagent_orchestration.helper_max_steps 20
alysis config set subagent_orchestration.helper_timeout_s 120
```

Built-in subagents:

- `explorer` (read-only repository investigation with concise evidence-first findings)
- `dependency-scout` (read-only external dependency research grounded in pinned local versions and
  cited web evidence; `scout` is an alias)
- `implementer` (write-capable implementation of one clearly scoped change, followed by verification)
- `frontend-engineer` (write-capable web UI implementation with responsive, interaction-state, accessibility, and evidence-based visual-QA requirements)
- `debugger` (diagnostic reproduction and root-cause isolation without source edits)
- `verifier` (evidence-based proof that a finished candidate meets its acceptance criteria, using the repository's real checks without source edits)
- `code-reviewer` (strict read-only review with verdict + blocking/non-blocking issues)
- `visual-designer` (opt-in production raster generation with a read-plus-generate sandbox; available only when image generation is enabled)

Built-in prompt behavior:

- gives each role a non-overlapping contract: investigate, implement general code, implement frontend UX, diagnose, verify a finished candidate, review, or generate raster assets
- keeps `explorer` and `code-reviewer` strictly read-only
- lets `debugger` run targeted diagnostics and verification while prohibiting repository edits; a host-observed material mutation degrades the run with `unexpected_workspace_mutation`
- lets `verifier` run the repository's authoritative checks while prohibiting edits, then return an evidence-backed pass, fail, or inconclusive verdict; `debugger` finds the cause of an unexplained failure, while `verifier` proves a finished candidate
- requires `shell_run` for `debugger` and `verify_run` for `verifier`; if the requested mode or
  host capability removes a required tool, launch is refused before the child runs and reports the
  smallest sufficient mode
- lets `implementer` make the smallest scoped change allowed by the parent session
- makes `frontend-engineer` use the repository's existing frontend stack and explicitly cover responsive layout, accessibility, and loading/empty/error/disabled states
- prevents `frontend-engineer` from calling `image_generate`; raster work belongs to `visual-designer`
- prevents `frontend-engineer` from returning a generator prompt as a substitute for an image request
- limits `visual-designer` to read-only repository tools plus `image_generate`, so it cannot edit application source or existing assets
- requires `visual-designer` to generate an actual file for in-scope bitmap requests; users never need to ask it to call a function or tool
- degrades a `visual-designer` run instead of accepting success when `image_generate` is missing from its sandbox or no successful `image_generated` event proves that an artifact was written
- requires both visual specialists to distinguish technical/build validation from visual inspection and to report `Visual QA` as pending when no real browser/vision evidence was inspected
- requires concise evidence-backed handoffs instead of action transcripts
- requires agents to report verification actually performed and remaining uncertainty or blockers

### Evaluation-informed working methods

For uncommitted work, `code-reviewer` starts with `git_status` and a path-scoped `git_diff`. It
reads only the surrounding lines needed to interpret a hunk and the tests covering the changed
behavior through `fs_read_lines`; `fs_read` is absent from this role's sandbox. Whole-file reads
and `git_history` are absent from this role's sandbox; other readonly roles retain their history
tool when history is part of their task.
`verifier` follows the same diff-first discovery rule before choosing authoritative checks.

Treat review and verification as separate decision points when review findings may change the
tree: review, fix, then verify. Reuse a child's evidenced verification while the tree remains
unchanged instead of running the same command again. Host-structured `verify_run` results are the
authority for pass/fail; truncated raw output can add detail but cannot contradict `all_passed`.

Direct implementation is normally cheaper for one scoped change because a delegated implementer
pays for fresh context and its own cache. Delegate writes when independent work can run in
parallel, when a clean isolated candidate enables verify-before-apply, or when risk justifies that
isolation.

For repository-mapping tasks, `explorer` ends its report with a compact `Map:` of up to 15
`path - one-line role` entries. The parent should navigate from that handoff and use targeted
confirmation reads instead of repeating the repository walk. Truncated `fs_read` and
`fs_read_lines` results report the total line count, returned range, exact next non-overlapping
range, and any offload artifact reference so continuation does not overlap prior reads.

When a managed-browser capability is attached, `verifier` also receives the approved browser
inspection surface plus `browser_start`, `browser_navigate`, `browser_click`, and `browser_type`.
For a user-facing web change with a runnable target, it starts the app with the managed service
tools, navigates to that session-owned service's `preview_url`, performs a browser smoke of the
changed flow, records snapshots or screenshots as `Visual QA` evidence, and stops the service.
Preview authorization is recomputed from the session's active durable-service registry: it matches
scheme, host, and port exactly while allowing paths below that origin. Other loopback URLs remain
blocked. The verifier remains non-writing, browser actions keep host approval, and browser artifacts
remain session artifacts rather than repository changes.

### External dependency research

`dependency-scout` handles questions the repository cannot answer, such as version-specific
library APIs, migrations, upstream behavior, and advisories. `explorer` remains the role for local
repository questions. A scout first reads manifests and lockfiles to pin the local version, then
uses the real `web_search` or `web_fetch` tools and separates path-cited repository facts from
URL-cited external claims.

The role is callable only when web tools are enabled and web search has a configured, ready
adapter. For example, set `web_tools_enabled=true`, configure a supported search adapter, keep
`web_search_mode` enabled, and start a new session. Capability context and launch errors report the
concrete missing configuration and whether a new session is required. The exception is
definition-scoped: only a
readonly child that both requires `external_research` and allowlists the web tools receives them;
ordinary children and depth-2 helpers do not.

A scout cannot succeed on model memory alone. Its child event store must contain at least one
successful `web_search` or `web_fetch` result; otherwise the host degrades the run with
`required_capability_evidence_missing`. Its report includes the answer, pinned version context,
source URLs, confidence, and anything it could not verify.

### Image generation setup

Image generation is disabled by default because calls can incur a separate provider charge. The
`visual-designer` agent and `image_generate` tool are omitted from callable roles until enabled.
They remain discoverable through capability errors and the main agent's capability context, with
an actionable reason instead of an "unknown subagent" error.
Mode-gated sessions use the effective tool surface for that context, so a readonly session reports
the concrete mode switch required instead of advertising the visual role as callable.

Configure an OpenAI-compatible image endpoint and credential, then start a new chat session:

```powershell
$env:ALYSIS_IMAGE_API_KEY = "<image-provider-key>"
alysis config set image_generation.enabled true
alysis config set image_generation.model gpt-image-1
# Optional when the active provider profile is not the image provider:
alysis config set image_generation.base_url https://api.openai.com/v1
```

Instead of `ALYSIS_IMAGE_API_KEY`, set
`image_generation.api_key_env` to the name of a provider-specific environment variable. If neither
is configured, Alysis Code reuses the active session credential. Generation accepts PNG, JPEG, and
WebP output paths, creates one to four new files, validates decoded image dimensions and format,
and never overwrites an existing file.

Optional safety and provider limits are configurable through
`image_generation.timeout_s`, `image_generation.max_images_per_call`,
`image_generation.max_image_bytes`, and `image_generation.max_pixels`.

### Exercise a built-in through chat

Ask the main agent to delegate to a named role when testing one specialist at a time:

```text
Delegate this to frontend-engineer: implement the settings card in src/web/... using the existing design system. Cover mobile/desktop, loading/error/disabled states, keyboard access, and run the focused frontend checks. Report visual QA evidence separately.

Delegate this to visual-designer: create one transparent product illustration for the empty state. Inspect existing assets under src/web/assets first, write a new PNG under that convention, and report dimensions/hash plus visual-QA status. Do not edit source code.
```

The visual role becomes callable only in a session started after image generation was enabled.
The task itself should state only the desired creative outcome and output constraints; never
instruct the agent to call `image_generate`.

Discoverability:

- when subagents are enabled, the `subagent_run` tool schema exposes autonomously routable subagent names in `name.enum`
- enum values are built from the loaded registry, so available custom subagents with automatic routing visibility are included automatically
- the main agent also gets a pinned `<subagent_context>` message with autonomously routable and capability-gated subagents plus delegation guidance, so it can decide when to delegate or report a grounded blocker
- repo turns also get a bounded turn-scoped `<subagent_turn_context>` containing the routable roles when subagents are available; natural-language delegation requests are advisory to the model
- use `--no-subagents` or `alysis config set subagents_enabled false` for a hard disable
- interactive repo execution turns that spend multiple read-only steps without subagent delegation receive a runtime nudge to delegate focused exploration or move to implementation/verification
- when enabled, the main system prompt also adds short delegation guidance for autonomous subagent_run use (disabled sessions keep baseline prompt behavior)

Routing visibility controls autonomous discovery, not whether a custom role remains registered:

- `routing_visibility: auto` is the default for built-in and custom roles; these roles can appear in the tool enum and parent routing context.
- No built-in currently uses `routing_visibility: manual`.
- Custom subagents can set `routing_visibility: auto` or `routing_visibility: manual` in
  frontmatter. Manual custom roles stay registered but are omitted from the autonomous enum and
  parent routing context. Unknown values normalize to `auto`.

Custom subagents can be defined with YAML frontmatter + markdown body in:

- project: `./.alysis_agents/*.md`
- user: `~/.config/alysis/agents/*.md`

Frontmatter example:

```md
---
name: api-reviewer
description: API-focused reviewer
mode: readonly
allow_workspace_writes: false
allow_tools:
  - fs_read
  - fs_read_lines
  - fs_list
  - symbol_search
  - search_rg
deny_tools:
  - shell_run
# Claude-style aliases are also supported:
# tools: [fs_read, fs_read_lines, fs_list, symbol_search, search_rg]
# disallowedTools: [shell_run]
model_role: review
routing_visibility: auto
---
You are a strict API reviewer. Focus on breaking changes and missing tests.
```

Notes:

- Custom markdown bodies are treated as scoped subagent guidance, not as a full system-prompt replacement.
- Base `SYSTEM_PROMPT` is always preserved for subagent sessions.
- Built-in/code-owned subagent prompts may add trusted system-layer guidance; user-defined subagents provide scoped guidance and cannot replace the base system prompt.
- Main agent gets only the final subagent result, not intermediate nested tool outputs.
- Only eligible depth-1 writers can invoke bounded non-editing helpers; depth-2 recursion is blocked.
- Tool permissions are sandboxed by per-subagent allow/deny lists.
 - The parent catalog labels each autonomously routable role with
  `parallel_batch_eligible=yes|no`. This is scheduling metadata only: choose roles by task fit;
  batch eligibility does not determine which role to pick. Shared-view calls are eligible only in
  exact `readonly` mode; isolated calls are eligible in any post-clamp mode. Same-batch calls run
  concurrently only when every call is eligible. The host runs at most four concurrently, queues
  any excess, preserves result order, and prevents queued children from launching after parent
  cancellation. Shared `review`, `auto`, and `fullaccess` calls remain sequential.
- Nested tool-event IDs include the per-invocation subagent run ID, so parallel calls to the same
  role cannot overwrite one another's UI/protocol state even when providers reuse child-local IDs.
 - Parent cancellation reaches both synchronous model-directed batches and background child runs.
- Write-capable child runs are wrapped in a host-owned before/after workspace reconciliation.
  Snapshot-observed changes are unioned with tool-reported paths and returned as
  `touched_repo_paths`, `material_touched_repo_paths`, mutation classifications, and `effects` on
  success, failure, cancellation, incomplete, and degraded outcomes.
- Set `allow_workspace_writes: false` on a custom diagnostic role to make material workspace
  changes an `unexpected_workspace_mutation` degradation. This detects and reports a contract
  violation; it does not automatically revert the affected paths.
- The billable `image_generate` capability is exposed only when image generation is enabled.
- Each child session receives an exact model-visible catalog of the filtered
  tool names it may call, with required arguments. The catalog is built after
  permission filtering and tells the child not to invent aliases; unknown-tool
  recovery remains active inside the child session.
- Default subagent mode is `readonly` unless explicitly overridden.
- Review-mode subagents suppress nested UI chatter but forward approval prompts through the parent session surface.
- Non-interactive subagent sessions still fail fast when a nested write/shell/verify action would require confirmation.
- Ordinary subagents have no default step ceiling. They continue until their
  delegated task completes, they are cancelled or blocked, or they encounter a
  fatal error. The optional `max_steps` tool argument adds an explicit safety
  limit for that child only.
- Child repetition safeguards compare the complete sanitized tool name, arguments, and outcome.
  Identical calls whose outcomes change are legitimate re-checks and reset the consecutive count.
  Tools that embed wall-clock fields in otherwise stable outcomes, such as background-process
  snapshot or wait payloads containing `elapsed_ms`, cannot produce identical fingerprints and are
  therefore invisible to this sensor; the finite child run deadline remains the guard for them.
- Every ordinary subagent has a finite wall-clock ceiling. Its effective
  deadline is the earlier of the active parent deadline and the
  `subagent_timeout_s` fallback (900 seconds by default). An earlier parent
  deadline is reused exactly, so delegation cannot extend the parent run; when
  the parent has no active deadline, the fallback supplies the child ceiling.
  Configure it with, for example,
  `alysis config set subagent_timeout_s 600`. The value must be finite and
  greater than zero.
- Child LLM and tool-call timeouts clamp against that resolved child deadline.
  Lifecycle telemetry records `subagent_timeout_s`, `resolved_timeout_s`,
  `resolved_deadline_source`, and the full resolved `deadline` snapshot.
- Next-call admission estimates duration from the median of the latest five completed calls rather
  than one historical maximum. Telemetry records the estimator, window, samples, and estimate. A
  call that overruns into the reserve proceeds directly to finalization; admission is still refused
  when the robust estimate cannot fit the total hard time remaining.
- If too little hard time remains, or the parent has entered the soft
  finalization window, `subagent_run` and `subagent_spawn` refuse to launch and return the usual
  error-shaped result with deadline metadata such as
  `failure_category: "deadline"`, `deadline_prevented_launch`,
  `deadline_start_decision`, and `remaining_seconds`.
- Release changes to subagent deadline propagation should follow the focused
  [release checklist](release_checklist.md). Maintainers integrating this completed stack should
  also follow the [subagent merge runbook](subagents/RELEASE_RUNBOOK.md).
- For precise inspection, prefer `symbol_search` for Python/JS/TS symbol navigation, `search_rg` for broader text hits, `fs_read_lines` to read the exact surrounding range, and `fs_read` when broader file context is needed.
- For history or regression questions, prefer `git_history` over raw shell commands.
- Subagent execution mode is capped by the parent session mode (no privilege escalation): readonly < review < auto < fullaccess. Built-in definitions additionally restrict their visible tools; for example, `debugger` has diagnostic tools but no direct file-edit tools.
- Subagent token/cost usage is replayed into the parent session one child model call at a time, including failed subagent runs, so `/usage` and the chat HUD preserve call counts and api-vs-estimate attribution.

## Future directions

The remaining orchestration work is deliberately narrower: parallel scheduling for bounded
helpers, snapshot-backed isolated views for non-Git roots, opt-in auto-apply policies, and richer
merge strategies than the current conflict-checked patch application.
