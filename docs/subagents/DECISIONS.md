# Subagent engineering decisions

This log records assumptions made during the autonomous subagent audit and improvement mission.

## 2026-08-21

### D023 - Refuse uneconomic work before launch and preserve useful child evidence

The continuation-backed subscription Responses transport made four keepalive pings consume about
213k uncached input tokens while caching only a constant 3,456-token prefix, to protect a roughly
49k-token cold wake. Refuse to arm keepalive for response-continuation and subscription/gateway
transports, with one recorded `keepalive_unsupported_transport` warning. Keep the default-off
experiment only for stateless full-request transports where replay and the next parent call share
the same cache stream.

Use the parent's robust recent-call estimate to reject a nested helper unless its remaining child
time can fit two estimated model calls. This prevents 90-171 second helper allocations from being
spent on providers whose ordinary call is about 200 seconds, while leaving the writer free to work
directly. A deadline-blocked child event must agree with the terminal deadline summary.

Preserve evidence across recovery and reporting boundaries: seed resumed sessions with still-valid
content-hashed read-ledger ranges; prefer structured authoritative verification over truncated raw
output; and partition cancellation results into actually cancelled, already terminal, and unknown
runs. Finally, remove `git_history` from `code-reviewer` after three evaluations showed prompt-only
prohibitions did not stop history use for uncommitted work. State the delegation economics directly:
a fresh implementation child is justified by parallelism, clean verify-before-apply, or isolation
risk, not by a single scoped edit. These changes remain in neutral agent/session infrastructure;
Forge and swarm behavior is unchanged.

## 2026-08-20

### D022 - Enforce read reuse, but make cache keepalive an opt-in experiment

Keep a content-hashed read ledger inside each parent or child session. Unchanged
exact ranges become compact notices and partial overlaps return only unread lines;
file changes and session writes invalidate the affected path. Keep `force=true` as
the loss-safe escape hatch and a default-on kill switch for compatibility. Ledgers
do not cross session boundaries because one child's evidence is not another
model's transcript.

The evaluation also measured a roughly ten-minute synchronous child followed by
about 3.4 million uncached parent input tokens. Permit a parent-prefix refresh only
while that parent is blocked on synchronous children, using the exact last request
messages/tools, the provider's minimum 16-output-token allowance, temperature zero,
and a 240-second interval. A
ping is accounting-visible but transcript-invisible, and deadline finalization and
cancellation always take priority. Keep this mechanism disabled by default until a
real-provider rerun proves that its cached-read and bounded-output cost is lower
than the cold parent reread it avoids. This remains neutral session/turn/provider
infrastructure; Forge and swarm behavior is unchanged.

### D021 - Keep retained child identity stable and make execution contracts host-enforced

Use one parent-session run-ID registry for synchronous and background children so a retained
candidate's advertised resume command always resolves. The scheduler's synchronous bookkeeping is
additive and emits no replacement lifecycle events. Applying a non-success candidate now requires
an explicit `acknowledge_incomplete=true`, preserving recovery without allowing a partial result to
look like an ordinary successful apply.

The third live evaluation showed deadline admission using the single longest prior call: one
201-second outlier rejected useful work despite roughly 316 seconds of hard time and 196 seconds of
normal-work allowance. Estimate the next call with the median of the latest five completed calls,
record those inputs in telemetry, and retain the hard-total-time refusal. If an admitted call
overruns the estimate into the reserve, the next action is finalization. This changes neither the
explicit 120-step ceiling in that run nor the ordinary-child step-default policy.

Make execution-role requirements structural: `verifier` requires `verify_run`, `debugger` requires
`shell_run`, and custom definitions may declare the same contract. Refuse a launch before running a
child when mode filtering or host capability removes a required tool. Finally, compute TUI child
elapsed time from the child's own start rather than the parent session epoch. All changes stay in
neutral orchestration, deadline, definition, and presentation modules; Forge and swarm
behavior remains unchanged.

### D020 - Make retained work recoverable and make verification sequencing explicit

Preserve the assistant report in the terminal `final` event when retained-worktree guidance is
appended, and expose enough host facts on incomplete children to resume rather than rebuild them.
The evaluation's incomplete implementer did not hit a step ceiling: it used 13 of 100 resolved
steps, but the deadline admission controller rejected the next estimated 181-second model call
with about 225 seconds hard time and 105 seconds normal-work time left after the finalization
reserve. Keep existing step defaults; report that `insufficient_normal_work_remaining` reason,
remaining time, ceiling, and the exact resume affordance instead of relabeling it as generic
deadline exhaustion.

Enforce reviewer economics in the sandbox as well as the prompt: remove whole-file `fs_read` only
from `code-reviewer`, retaining hunk-oriented `fs_read_lines` and every other readonly role's file
surface. Sequence review, any resulting fix, and verification as separate decisions, and reuse
fresh child evidence until the tree changes. Extend the same safety-analyzed repository discovery
used for Node scripts to Python-native pytest, unittest, Ruff, and mypy checks. These changes remain
inside neutral agent and verification modules; Forge and swarm behavior is unchanged.

## 2026-08-19

### D019 - Make live-evaluation evidence change delegation economics

Reject unrecognized tool-call modes instead of silently normalizing them; lenient mode fallback is
reserved for loaded definition and configuration inputs, where it is logged. This keeps a malformed
model invocation from appearing to succeed under a different sandbox.

Make review and verification diff-first, require repository-mapping explorers to return a compact
path map, and make truncated reads name the exact next range. The live evaluation showed that
re-reading whole files and repeating child discovery consumed substantially more context without
improving evidence. These contracts preserve targeted confirmation while removing overlapping work.

For user-facing web changes, let the non-writing verifier perform a managed-browser smoke when the
host capability exists. Only its definition-selected browser tools reach the child, existing host
approval and destination fences remain authoritative, and browser artifacts stay outside mutation
evidence. A build proves compilation, not the runtime route, loading, proxy, or interaction path.

### D018 - Ship external research only with real web evidence

Expose `dependency-scout` only when the top-level session has configured, ready web research tools,
and pass those tools only to a readonly child whose definition explicitly requires
`external_research`. A successful scout must record a successful `web_search` or `web_fetch` event;
otherwise its report degrades instead of presenting model memory as current research. Ordinary
children, depth-2 helpers, Forge workers, and swarm workers keep their existing
tool boundaries.

Keep inspection user-facing through `/subagent view`, reading a bounded tail from the child store
without adding model payload. Resume failed, incomplete, or cancelled work as a fresh linked run
under current clamps and budgets. An unreleased isolated worktree transfers to that new run so its
candidate remains continuous but still has one apply/discard owner.

### D017 - Keep steering and dependency scheduling parent-owned

Give each background child a scheduler-owned inbox drained by a generic child-session step hook.
This permits bounded parent steering without exposing a new transport or coupling neutral agent
modules to Forge or swarm code.

Represent dependent work explicitly with a `waiting` registry state. Dependencies advance through
the existing FIFO scheduler only after all prerequisites succeed; any other terminal outcome
cancels downstream work before launch. Pinned workspace resolution and the deadline start decision
therefore occur when a waiting child becomes runnable. Add one terminal rollup per parallel batch
or dependency chain so concurrency cost is observable without changing tool results.

### D016 - Keep orchestration flat while allowing bounded writer consultation

Flat parent-owned orchestration remains the primary model: the parent chooses independent work,
owns background lifecycle, and reconciles isolated candidates. A depth-1 writer may nevertheless
need repository research, review, verification, or debugging while its implementation context is
hot. Letting it synchronously consult a small non-editing helper roster avoids a parent round-trip
without granting another writer, background lifecycle tools, or recursive orchestration.

Helpers share the writer's current root, run one at a time, and stop absolutely at depth 2. Per-child
count, step, and deadline limits bound the extra work. Usage replays helper-to-writer and then
writer-to-parent exactly once, while an additive `helper_runs` summary makes the nested cost visible.
Forge execution and swarm workers retain their existing behavior, and the
implementation remains inside subsystem-neutral session, launcher, tool, and configuration modules.

### D015 - Make isolation, not role labels, the parallelism boundary

Schedule a same-response batch concurrently when every child is either exact-readonly on the shared
tree or owns an isolated worktree. Shared writers remain sequential. Isolated background writers are
also safe under the existing FIFO cap, and their completed candidates remain retained until explicit
apply/discard or session close.

Allow non-writing children to borrow a completed candidate through `workspace_from_run`. The source
worktree is pinned against apply/discard for the verifier's lifetime, providing an independent
implement-verify-apply flow without copying patches into the parent or crossing the neutral
subagent/workspace boundary.

### D014 - Isolate child edits at HEAD and require explicit reconciliation

Add an opt-in `workspace_view=isolated` backed by a session-owned Git worktree provider. Each run
starts from the parent's current `HEAD`, records the dirty parent paths it cannot see, and returns
only a bounded patch summary while persisting the complete patch as an internal session artifact.
The default shared view and existing mode clamps remain unchanged.

Retain isolated work until an explicit apply/discard decision or session close. Apply is serialized,
checks the patch before changing the parent, keeps conflicts for inspection, and never commits.
Keep the implementation behind the neutral `workspace_isolation` and `git_evidence` boundary; the
subagent path does not import Forge/swarm modules, and its worktrees never enter a
Forge run layout.

## 2026-08-18

### D013 - Background only independent readonly investigations

Add a session-owned child scheduler so the parent can start independent repository investigation
and continue useful work before collecting the result. One shared executor now serves background
children and the existing same-response readonly batch path. Background concurrency follows
`subagent_orchestration.max_background_children`; same-response batches retain their separate cap
of four and ordered results.

Keep background spawning exact-readonly in Phase 1. Parent and child still share a working tree,
so allowing a background writer before workspace isolation and reconciliation would introduce
nondeterministic races and ambiguous ownership. Non-readonly requests remain available through the
unchanged synchronous `subagent_run` contract.

Make child ownership explicit: every spawned child must be joined or cancelled before its parent
turn ends. The host gives the model two opportunities to do this itself, then applies the configured
wait-or-cancel policy. Parent cancellation always propagates to running children and prevents queued
children from launching. Usage is replayed incrementally with a per-child cursor, and lifecycle
events remain additive so existing synchronous consumers keep their contracts.

## 2026-08-17

### D011 - Separate verification evidence, routing visibility, and batch scheduling

Add `verifier` as the evidence-based proof step for a finished candidate change. It runs the
repository's real checks without editing the workspace and reports pass, fail, or inconclusive
against explicit acceptance criteria. This keeps verification distinct from `debugger`, which
investigates an unexplained failure to find its cause.

Keep `test-strategist` available but set its routing visibility to manual. It was over-selected
when advertised to autonomous routing, while its plan-only output displaced implementation and
real verification. Removing it would also remove useful explicit workflows, and no
template-distribution mechanism currently exists to replace the built-in role. Direct
`/subagent test-strategist <task>` invocation and picker discovery therefore remain supported.

Rename the parent catalog's `parallel_safe` label to `parallel_batch_eligible`. Benchmark behavior
showed that the model was treating concurrency metadata as role-selection advice. The new name and
guidance state that eligibility affects scheduling only; task fit determines which role to use.

### D012 - Extract neutral orchestration foundations without crossing subsystem boundaries

Move the interactive subagent execution engine behind a dedicated launcher and move reusable
workspace-isolation and git-evidence machinery into subsystem-neutral modules. The existing
Forge/swarm modules keep compatibility re-exports, so current imports, event
contracts, stores, and runtime behavior remain unchanged.

Interactive subagents and Forge/swarm remain distinct subsystems. The subagent path
must not import `swarm_backend`, activate Forge run layouts, or read or write Forge
stores. Any subsystem-specific adapter needed by the neutral launcher is injected by its
caller instead of creating a cross-subsystem import. Background-child configuration is additive
scaffolding only in this phase and is not consumed by the runtime yet.

## 2026-07-17

### D001 — Preserve the pre-existing dirty worktree

The starting worktree already contained uncommitted subagent, routing, capability, TUI, documentation, and test changes. They are treated as user-owned in-progress work. The audit uses that working tree as the current product baseline, avoids reverting or overwriting it, and commits phase artifacts by explicit path. Relevant pre-existing changes may only enter an implementation commit after they are reviewed and verified against the mission acceptance criteria.

### D002 — Use the local mock provider for the reproducible benchmark

No live model-provider credential is present in the environment. End-to-end benchmark cases therefore use Alysis Code's real CLI and nested-session runtime with the repository's local OpenAI-compatible mock server. This proves routing, tool schemas and permissions, child-session creation, parallel dispatch, result propagation, failure handling, deadlines, and terminal output without network cost or nondeterminism. Claims about open-ended model quality are kept separate and are supported by prompt-contract tests; a live-provider matrix remains an explicit follow-up when credentials are available.

### D003 — Build the missing failure matrix

The mission's known-failing-cases section is empty. Phase 1 will define approximately ten representative cases spanning role selection, repository lookup, multi-file implementation, diagnostics, review/test planning, parallel delegation, unavailable or invalid roles, empty/degraded results, deadline behavior, cancellation, and TUI presentation. The same fixed cases and expectations will be reused in Phase 5.

### D004 — Pin comparative evidence

Comparative research reflects upstream repositories and official documentation as observed on 2026-07-17. Repository commit hashes and direct source URLs are recorded where available so future changes do not silently alter the comparison.

### D005 — Make artifact-specialist success evidence-based

`visual-designer` is treated as an artifact-producing role, not a prompt-writing role. A run can report success only when its required generation tool is present and the child session records the capability's durable success event (`image_generated`). A direct wrong-medium or prompt-only handoff therefore degrades instead of being mislabeled successful. This stricter role boundary prevents polished text from masquerading as the requested bitmap while leaving the raw child audit trail intact.

### D006 — Keep the reviewed name-based TUI identity

The pre-existing TUI change that identifies subagents by name, shared nesting marks, accent colour, and activity tagline is retained. Per-agent glyphs are removed consistently from picker, start, result, and hint surfaces. The event/bridge payload contract is unchanged; focused classic CLI and TUI tests verify that the decision is presentation-only.

### D007 — Use a conservative finite ordinary-subagent timeout

Ordinary subagents default to a 900-second (`subagent_timeout_s`) wall-clock fallback. Fifteen minutes is long enough for multi-step repository work while eliminating the prior unbounded hang when the parent had no deadline. The effective child ceiling is still the earlier active parent deadline, and a parent already in finalization or exhausted is reused exactly so its soft-stop state cannot be reset by deriving a fresh child clock.

### D008 - Treat child reports as untrusted evidence

Subagent reports may contain role-looking markup or instructions that must not acquire parent authority. Recognized role and Alysis Code harness tags are escaped, narrow instruction/permission/tool-demand patterns are replaced with explicit blocked markers, and structured bounded safety metadata is attached to parent-visible results. Nested assistant streaming is buffered until message completion so split tags cannot cross the trust boundary unscreened. The child session keeps its original audit events; every parent-visible success, degraded, failure, transcript, tool result, and nested display surface receives only the screened report. A report containing only wrappers, blocked instructions, or a bare acknowledgement is non-substantive, while factual values such as `42`, `False`, paths, and genuine findings remain valid evidence.

### D009 - Gate the POSIX PTY matrix by capability

`test_show_owl_matrix.py` exercises the POSIX shell script through `pty` and therefore depends on `termios`, which is unavailable on native Windows. The matrix now skips at collection when those POSIX modules are absent; POSIX hosts still import and run the same scenarios unchanged. Windows coverage remains with the separate platform-neutral terminal matrix tests.

### D010 - Measure the release gate against an isolated pre-mission baseline

The repository's full suite is not globally green at the starting commit. Five deterministic tests fail identically at pre-mission commit `83080720` and at the final implementation head: one classic `/model` test, one 80-column welcome-banner test, two managed-execution budget tests, and one provider stream-restart telemetry test. Three additional real-CLI tests fail only in the 6,701-test shared process and pass together immediately when isolated at the final head. They are reported as order/resource-sensitive open issues, not hidden or counted as passes. "Zero regressions" therefore means no new deterministic failure relative to an isolated `83080720` archive, plus green focused coverage for every changed contract. The authoritative full run uses Linux, a declared target platform; native Windows separately verifies the complete surface-rendering file and capability-skips the POSIX-only PTY shell matrix.
