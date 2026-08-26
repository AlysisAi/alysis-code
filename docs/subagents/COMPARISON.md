# Subagent implementation comparison

Date researched: 2026-07-17

This comparison focuses on concrete task-delegation mechanics rather than product positioning. OpenCode and Kilo Code were inspected at pinned commits; Claude Code is closed source, so its section relies on current official documentation and documented release behavior.

## Sources and pins

### OpenCode

- Repository commit [`4bffbb655f5e2886df502b2305db03acd4138fff`](https://github.com/anomalyco/opencode/commit/4bffbb655f5e2886df502b2305db03acd4138fff), `dev`, 2026-07-17 10:15 UTC
- [Official agent documentation](https://opencode.ai/docs/agents)
- [Task implementation](https://github.com/anomalyco/opencode/blob/4bffbb655f5e2886df502b2305db03acd4138fff/packages/opencode/src/tool/task.ts)
- [Task prompt](https://github.com/anomalyco/opencode/blob/4bffbb655f5e2886df502b2305db03acd4138fff/packages/opencode/src/tool/task.txt)
- [Agent registry](https://github.com/anomalyco/opencode/blob/4bffbb655f5e2886df502b2305db03acd4138fff/packages/opencode/src/agent/agent.ts)
- [Subagent permission derivation](https://github.com/anomalyco/opencode/blob/4bffbb655f5e2886df502b2305db03acd4138fff/packages/opencode/src/agent/subagent-permissions.ts)
- [Task tests](https://github.com/anomalyco/opencode/blob/4bffbb655f5e2886df502b2305db03acd4138fff/packages/opencode/test/tool/task.test.ts)

### Kilo Code

- Repository commit [`2c070e6e6f8387329f0243708ef82a4920502ec7`](https://github.com/Kilo-Org/kilocode/commit/2c070e6e6f8387329f0243708ef82a4920502ec7), `main`, 2026-07-17 09:15 UTC
- [Official custom-subagent documentation](https://kilo.ai/docs/customize/custom-subagents)
- [Official orchestration documentation](https://kilo.ai/docs/code-with-ai/agents/orchestrator-mode)
- [Kilo Task policy](https://github.com/Kilo-Org/kilocode/blob/2c070e6e6f8387329f0243708ef82a4920502ec7/packages/opencode/src/kilocode/tool/task.ts)
- [Task implementation](https://github.com/Kilo-Org/kilocode/blob/2c070e6e6f8387329f0243708ef82a4920502ec7/packages/opencode/src/tool/task.ts)
- [Subagent permission derivation](https://github.com/Kilo-Org/kilocode/blob/2c070e6e6f8387329f0243708ef82a4920502ec7/packages/opencode/src/agent/subagent-permissions.ts)
- [Task tests](https://github.com/Kilo-Org/kilocode/blob/2c070e6e6f8387329f0243708ef82a4920502ec7/packages/opencode/test/tool/task.test.ts)

### Claude Code

- [Official subagent documentation](https://code.claude.com/docs/en/sub-agents)
- [Official parallel agents overview](https://code.claude.com/docs/en/agents)

Version note: current Claude Code documentation calls the tool `Agent`; `Task(...)` remains an alias after the 2.1.63 rename.

## OpenCode

### Definition and discovery

Agents can be configured in `opencode.json` or Markdown under project `.opencode/agents/` and user `~/.config/opencode/agents/`. Definitions support:

- mode (`primary`, `subagent`, or `all`);
- description and prompt;
- model, temperature, top-p, and provider options;
- step limit;
- permission rules;
- hidden/disabled state and display color.

Automatic delegation is description-driven, while `@agent` forces an explicit choice. `permission.task` uses ordered glob rules to decide which child types a parent can see or invoke.

The official documentation currently mentions General, Explore, and Scout. At the pinned commit, the registry defines General and Explore and no Scout implementation was found. That docs/code mismatch is upstream evidence for keeping role documentation under executable contract tests.

### Context isolation and task prompt

A Task call creates a persistent child `Session` with its own message history, agent name, permissions, and `parentID`. A fresh child receives the delegated prompt, its normal environment/instruction layers, and agent configuration—not the parent transcript.

The Task prompt makes task-brief quality explicit: state whether edits are expected, how to verify them, and exactly what the child should return. This is close to Alysis Code's current self-contained-brief contract.

Supplying `task_id` resumes the same child history. At the pinned commit, OpenCode does not verify that the session being resumed belongs to the calling parent, and a missing ID silently creates a fresh child rather than returning not-found.

### Permissions and model selection

Permissions support allow/ask/deny decisions, ordered tool and command patterns, external-directory rules, MCP wildcards, and per-agent overrides. `todowrite` and `task` are denied in children by default unless the definition explicitly mentions them. Parent session denies and external-directory rules become child ceilings.

Parent *agent* restrictions are not generally child ceilings. The pinned test suite deliberately allows a write-capable custom child to edit when invoked from Plan, although Plan denies the built-in General child. Alysis Code's parent-mode clamp is simpler and safer.

An explicit child model wins; otherwise the child inherits the invoking assistant message's provider/model. Agent-level `steps` forces a final text response after the configured iteration count.

### Result, concurrency, and failure handling

Foreground execution returns the last text part inside a task wrapper and includes the task ID for continuation. The parent prompt states that the child result is not directly user-visible and must be synthesized.

There is no substantive-result validator: an empty last text part can still be reported as a completed task.

Multiple Task calls in one model response run concurrently. Experimental background subagents can return immediately, inject completion/error later as a synthetic parent prompt, and accept updates through the same task ID. Nesting is supported up to a configurable depth, default one, when child permissions expose `task`.

Parent abort cancels foreground and background children, and removing/cancelling a parent recursively cancels descendant jobs. There is no Task-specific wall timeout; step limits and cancellation are the main runaway controls.

## Kilo Code

Kilo's current task runtime is OpenCode-derived but adds meaningful ownership, confinement, and error-fidelity protections.

### Definition and discovery

Definitions live in `kilo.jsonc`, project `.kilo/agents/*.md`, user `~/.config/kilo/agents/*.md`, or can be created with `kilo agent create`. The field set broadly matches OpenCode: description, mode, prompt, model/sampling settings, permissions, hidden/disabled state, steps, color, and provider options.

Code, Plan, and Debug can delegate automatically. The older Orchestrator mode is deprecated. Built-ins are General and Explore.

### Context, resumption, and UI

Each child has a separate session and history and receives only delegated task content. `task_id` resumes a child. Unlike the pinned upstream OpenCode path, Kilo rejects cross-parent resume attempts and refreshes parent permission restrictions and sandbox confinement on resume.

The VS Code client exposes read-only child-session viewer panels backed by persisted child transcript/event state. This is a useful companion to resumption and background work, but it is not required for safe one-shot delegation.

### Permissions and model selection

- Primary-only agents cannot be selected as children.
- Nested Task is unconditionally disabled.
- `question`, `interactive_terminal`, and normally `task` are removed from child tools.
- Caller edit, bash, and MCP denies are carried into the child as hard ceilings.
- Parent session denies, external-directory rules, and sandbox confinement are inherited on both creation and resume.

This closes OpenCode's Plan-to-write-capable-child escalation.

Model precedence is remembered per-agent CLI selection, child definition model, global subagent model, then parent model. Stale/unavailable overrides are skipped, and model variants are validated against the resolved provider.

### Result, concurrency, and failure handling

Kilo returns the final text part and task ID, like upstream. It has no visible empty/substantive report validator and no returned-report prompt-injection scan.

Concurrent foreground/background machinery is inherited; Kilo intentionally keeps nesting to one level. A terminal child assistant error crosses the boundary as an actual error rather than ordinary findings. Errors include a concrete resumable task-ID hint. Parent/child cancellation and removal stop jobs recursively.

Child cost deltas are propagated on success, background completion, resume, and abort with dedicated no-double-count coverage. Alysis Code's call-by-call usage replay is comparable, but it has no resume state to account for.

## Claude Code

Claude Code's implementation is closed source; the following is documented product behavior.

### Definition and context modes

Markdown definitions can be managed, CLI-provided, project-local, user-local, or plugin-provided, with documented precedence. Besides name/description, definitions may set tools/disallowed tools, model, permission mode, maximum turns, skills, MCP servers, hooks, persistent memory, background execution, effort, worktree isolation, and color.

Normal children receive a fresh context rather than the parent transcript or already-read files. Startup can include the role system prompt, generated delegation message, CLAUDE.md/memory hierarchy, a parent-start git snapshot, preloaded skills, and an optional sibling roster. Explore and Plan deliberately omit some heavy repository context. Fork-mode children can instead inherit the parent conversation.

`isolation: worktree` gives a child a temporary checkout and guards commands from escaping to the main checkout. This is the right prerequisite for parallel write-capable children.

### Permissions, hooks, and models

Children inherit the parent tool surface by default and then apply allow/deny composition. MCP servers can be scoped per child. Stronger parent permission modes remain ceilings. Agent-type allowlists restrict which children a coordinator can spawn.

Lifecycle hooks can validate individual child tool calls or reject child completion. Invalid tool allowlists fail launch with unresolved entries; earlier versions could launch tool-less children and produce confusing empty results.

Model resolution considers environment override, per-invocation selection, frontmatter model, then parent. Model allowlists are enforced with safe fallback. Per-agent effort and max turns are supported.

### Results, safety, and continuation

Only summarized relevant output returns to the immediate caller. Since 2.1.210, returned reports are scanned for instruction-shaped output; harness-like role/tag text is escaped and suspicious permission-setting content is marked. Since 2.1.199, API termination is a failure rather than ordinary findings; partial foreground output is preserved with a cutoff notice, and background failures retain their last output.

Background execution is now the default for independent work. Permission prompts identify the requesting child and surface in the main session. Completed agents can be resumed by ID/name through `SendMessage`, retaining their history and tools. Nested agents are supported to a fixed depth of five, and withholding the Agent tool prevents nesting.

## Gap table

| Capability | OpenCode | Kilo Code | Alysis Code baseline | Gap / decision |
|---|---|---|---|---|
| Custom definitions | Rich JSON/Markdown fields | Rich JSON/Markdown plus CLI/UI creation | Markdown with prompt trust, mode, exact tool allow/deny, model/model-role | Add definition-time max steps only if it solves a measured need; current per-call limit is adequate for P0/P1 |
| Autonomous discovery | Description-driven; Task permissions; `@` explicit | Same in normal Code/Plan/Debug | Model-visible enum, pinned catalog, explicit `/subagent` | Competitive; preserve capability-gated unavailable reasons |
| Fresh context | Persistent isolated child | Persistent isolated child | Fresh one-shot nested session | Competitive for isolation |
| Prompt composition | Agent prompt plus runtime layers | Same | Trusted built-ins appended; custom body is untrusted; exact tool catalog injected | Alysis Code's trust distinction is stronger |
| Parent permission ceiling | Session denies survive; parent agent policy may not | Caller edit/bash/MCP denies and sandbox survive | Parent mode clamps child, plus exact tool filtering | Alysis Code is at least as safe as Kilo and safer than pinned OpenCode |
| Fine-grained permission rules | Ordered tool/command/path/MCP patterns | Same | Exact tool-name allow/deny plus session mode | P2 for specialists needing command/path policy; not required for current built-ins |
| Model routing | Child override, else parent | Saved choice → child → global child → parent | Explicit model → model role → coding | Fix code-reviewer default role; otherwise good |
| Final report validation | Last text; empty can succeed | Last text; empty can succeed | Authoritative final event required; missing/non-authoritative is degraded | Alysis Code is stronger; also reject generic acknowledgements |
| Returned-output safety | No visible scan | No visible scan | Prompt-only trust guidance | Adopt Claude-style instruction-shape scan/escaping |
| Foreground parallelism | Concurrent calls | Concurrent calls | Up to four same-batch readonly/review children | Strong and safely conservative |
| Background jobs | Experimental lifecycle | Experimental lifecycle | None | P2; requires durable ownership, cancellation, notifications, and UI |
| Resumption | `task_id`, weak ownership checks | Validated owner-bound `task_id` | Child closes after one result | Largest capability gap; if added, use Kilo ownership validation and resume-safe permission refresh |
| Nesting | Configurable depth | Disabled | Disabled | Keep disabled; resumption/background offer more value with less orchestration risk |
| Worktree isolation | Same worktree | Sandbox confinement, same worktree | Ordinary children share workspace | Add optional worktree isolation before any parallel write children |
| Cancellation | Parent recursively cancels jobs | Parent recursively cancels jobs | Parent token is not passed to child | Immediate P0 |
| Wall-clock controls | Agent steps, no Task wall timeout | Agent steps, no Task wall timeout | Inherited absolute deadline, but none when parent is unbounded | Alysis Code is stronger when a deadline exists; add a bounded ordinary-child fallback |
| Usage accounting | Stored per child | Explicit deltas and no-double-count tests | Child calls replayed into parent, including failures | Competitive |
| Child-session UI | Navigable sessions | Read-only VS Code viewer | Live trace/badge/result attribution | Persistent viewer is only valuable with resume/background |
| Hooks/memory/skills | Broad plugin mechanisms | Broad ecosystem | No per-definition hooks/memory | P2; completion hooks are more valuable than persistent memory initially |
| Docs/code consistency | Pinned Scout mismatch | Pinned built-ins align | Registry/schema/tests visibly align | Preserve executable docs-alignment tests |

## Concrete lessons for Alysis Code

1. Propagate cancellation before doing anything else. A child that outlives the user's stop action invalidates every TUI safety claim.
2. Preserve authoritative-final-report validation and inherited deadlines; both are stronger than the pinned OpenCode/Kilo contracts.
3. Scan and escape instruction-shaped child output before inserting it into the parent transcript, while retaining the raw child log for audit.
4. Fail clearly when a custom allowlist resolves to unavailable/unknown tools instead of launching a confusing tool-less child.
5. Treat resumable children as the highest-value P2 capability. Ownership validation, permission refresh on resume, cost-delta accounting, and cancellation must be acceptance criteria—not follow-ups.
6. Do not enable parallel write children until worktree isolation exists and commands are proven unable to escape it.
7. Keep nesting disabled unless benchmark evidence demonstrates a need. Background work and resumption provide more practical value with a smaller trust surface.
