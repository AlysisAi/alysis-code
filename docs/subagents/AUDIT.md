# Subagent deep audit

Date: 2026-07-17

Baseline: `main` at `83080720`, plus the pre-existing uncommitted worktree described in [DECISIONS.md](DECISIONS.md)

Scope: ordinary planner/executor subagents used by `run` and `chat`; Forge workers are noted only where their behavior intersects this path

## Executive summary

Alysis Code has a sound core design: each subagent is a fresh nested `AgentSession`; project/user prompts are kept below the trusted system layer; the parent mode is a permission ceiling; tools are post-filtered and described to the child with exact names; recursion is disabled; readonly/review calls can run in a bounded parallel batch; the parent receives a structured final report rather than the child transcript; child usage is replayed; and the parent's absolute deadline is inherited.

The baseline is not release-ready. Two product bugs materially explain the reported wrong/empty/low-quality behavior:

1. Parent cancellation is not passed to the child. A soft-interrupted write-capable subagent may continue running and writing after the TUI says the turn stopped.
2. A successful read-only subagent answer in one-shot `run` is rejected by the unconditional empty-diff gate. The real CLI benchmark made five provider requests and emitted two `empty_diff_finalization_blocked` events for a request that only asked the explorer to inspect the repository.

The wider baseline also contains one deterministic failing test, has no credential-free nested-session smoke test, loses the parent's active workdir when delegating, routes `code-reviewer` through the coding model/temperature by default, lets the general implementer see the billable image tool when enabled, and validates only that a final report is non-empty—not that a generic acknowledgement such as “Done” is useful.

One context discrepancy matters: the mission describes a Textual TUI and out-of-process bridge. This checkout's main TUI is a `prompt_toolkit` application running `AgentSession.run_turn` on an in-process daemon worker thread (`cli_impl/tui/app.py:3`, `:1711`, `:2176`). No out-of-process TUI bridge was found. This audit and the planned fixes preserve the contract implemented by this tree.

## Baseline commands and results

The environment has no live provider key. Per D002, deterministic runs used the real CLI with `scripts.qa.mock_llm.MockLLMServer`.

| Command / exercise | Result |
|---|---|
| `python -m pytest -q tests/test_subagents.py tests/test_cli_subagents.py tests/test_tui_subagent.py tests/test_subagent_turn_policy.py tests/test_delegated_runtime_dispatch.py tests/test_prompt_payload.py tests/test_language_policy_prompts.py` | 152 passed, 27 warnings, 12.81 s pytest time |
| 20 targeted integration nodes covering parallel dispatch, hooks, compatibility, child unknown-tool recovery, Rich/TUI surfaces, prompt invariants, forced summaries, and post-explore behavior | 19 passed, 1 failed |
| `python -m pytest -q tests/test_tui_panels.py tests/test_tui_config.py tests/test_chat_slash_completer.py -k subagent` | 13 passed, 111 deselected |
| `python -m pytest -q tests/test_unknown_tool_recovery.py` | 1 failed, 6 passed |
| Real CLI classic chat: `/subagent explorer Read existing README.md and report its contents.` then `/exit`, with subagents enabled and the local mock provider | Exit 0 in 4.33 s; two provider calls; child exposed 11 readonly tools, called `fs_read`, and returned through the result panel |
| Real CLI one-shot: `run "Use a subagent to inspect the repository..."` with subagents enabled and the local mock provider | Exit 0, but five provider requests and two empty-diff repair cycles before forced acceptance |

The shipped mock-provider smoke does not currently exercise nesting: `tests/test_mock_provider_smoke.py:88` and `scripts/qa/raw_agent_proxy.py:470` set `subagents_enabled` to false.

## Complete lifecycle map

### 1. Definition, discovery, and availability

- `SubagentDefinition` holds name, description, prompt and trust, mode, allow/deny lists, required capabilities, and model routing fields (`src/alysis_code/subagents.py:20-31`).
- Built-ins live in `built_in_subagents` (`subagents.py:76-531`): `explorer`, `implementer`, `frontend-engineer`, `debugger`, `code-reviewer`, `test-strategist`, and capability-gated `visual-designer`.
- Registry loading starts with built-ins, then project `.alysis_agents/*.md`, then user agents (`subagents.py:630-642`). Later definitions overwrite earlier ones, so a user-global definition currently wins over an identically named project definition.
- Custom Markdown is parsed as untrusted guidance and supports Claude-style `tools` / `disallowedTools` aliases (`subagents.py:701-750`).
- Names and aliases are normalized at `subagents.py:645-656`; the active allow/deny tool filter is `subagents.py:659-676`.
- Capability readiness and actionable unavailable reasons are resolved at `subagents.py:534-628` and `capabilities.py:57-105` in the dirty baseline.

### 2. Main-session context and delegation decision

- Main-agent delegation policy is appended to the system prompt only for enabled top-level sessions (`agent/prompt_context.py:305-313`, `:2520-2549`).
- A bounded `<subagent_context>` lists names, purposes, and unavailable roles (`prompt_context.py:2584-2638`) and is injected during session creation (`prompt_context.py:3004-3014`).
- Turn-scoped policy detects explicit delegation requests and can require a repair attempt before accepting a final answer (`agent/turn/core.py:360-408`, `tests/test_subagent_turn_policy.py`).
- The public `subagent_run` schema tells the parent to provide a self-contained brief with goal, paths/symbols, prior findings, and result shape (`tools/registry.py:1703-1818`). The runtime replaces the name schema with the callable registry enum (`agent/tools_assembly.py:3715-3735`).

### 3. Spawn and child session construction

The central path is `_subagent_run` (`agent/tools_assembly.py:1283-1878`):

1. validate enabled/depth/config/name/task;
2. resolve definition and capability state;
3. refuse launch when the inherited deadline is too close;
4. resolve the requested/declared mode under the parent-mode ceiling;
5. deep-copy configuration, select model and temperature, and force child routing to `code_only`;
6. resolve the child step budget;
7. emit start telemetry and wrap the parent surface in `NestedSubagentSurface`;
8. create a fresh `RuntimeKind.SUBAGENT` session with recursion and compaction disabled (`tools_assembly.py:1482-1514`);
9. post-filter its tools and inject an exact model-visible catalog (`:1544-1600`);
10. call `sub_session.run_turn(task)` (`:1627`);
11. replay usage, close the child, classify the final report, emit end telemetry, and return structured JSON.

### 4. Context passed to the child

The child does not receive the parent transcript. It receives:

- the normal Alysis Code base system prompt and repository bootstrap context;
- workspace summary, repository conventions, and applicable skill context built by normal session creation;
- the built-in role prompt as trusted system append, or a project/user role body as an untrusted prompt prelude (`tools_assembly.py:1475-1513`, `prompt_context.py:2561-2572`);
- an exact catalog of the tools left after sandboxing (`tools_assembly.py:612-648`, `:1589-1592`);
- the raw delegated task as the new turn instruction (`agent/turn/core.py:1263`, `:1754`).

This isolation is appropriate, but it makes the quality of the parent-composed task brief load-bearing.

### 5. Tool permissions and execution mode

- Top-level readonly mode exposes only the bounded inspection surface; MCP and custom tools are not exposed to ordinary nested subagents (`tools_assembly.py:691-739`, `custom_tools/session.py:16-29`, `mcp/manager.py:35-51`).
- The child mode is capped by the parent and never reaches `fullaccess` unless the parent is already `fullaccess` (`tools_assembly.py:1221-1236`).
- Definition allow/deny lists are applied after child construction, and `subagent_run` is always removed (`subagents.py:659-676`, `tools_assembly.py:1547-1557`).
- Readonly roles receive explicit read/navigation tools. `debugger` additionally receives `shell_run` and `verify_run`. `implementer` intentionally uses an empty allowlist, which means every tool exposed by its child mode; that currently includes `image_generate` when the capability is enabled.
- No child can recursively delegate because child construction sets `subagents_enabled=False` and depth 1 (`tools_assembly.py:1509-1511`).

### 6. Model and budget selection

- Resolution order is an explicit definition model, then definition `model_role`, then the coding role (`tools_assembly.py:1261-1281`).
- The matching role temperature is copied into both `temperature` and `coding_temperature` (`tools_assembly.py:1383-1391`).
- Every built-in currently omits `model_role`; therefore even `code-reviewer` resolves through coding rather than the configured review model/temperature.
- Step budgets use the shared resolver (`tools_assembly.py:1393-1417`). Current policy deliberately makes ordinary children unlimited by default; explicit `max_steps` remains available (`tests/test_subagents.py:1021-1204`).
- The child receives the same absolute deadline object as the parent (`tools_assembly.py:1512`). With no parent deadline, there is no ordinary-subagent wall-clock ceiling.

### 7. Streaming and TUI rendering

- `NestedSubagentSurface` scopes nested tool IDs, tracks completed child steps, forwards approvals, and forwards nested tool/reasoning events to the parent surface while hiding normal child assistant chatter (`surface/hidden_surface.py:186-420`).
- The `prompt_toolkit` TUI tracks concurrent activity by worker thread plus role name and pins the active name in the footer (`cli_impl/tui/surface.py:714-799`). Compact trace hides nested tool detail; full trace exposes it (`surface.py:626-708`).
- Explicit `/subagent` parsing and status handling live in `cli_impl/chat/commands.py:740-792`; execution/result display live in `cli_impl/chat/loop.py:758-833` and `cli_impl/commands/chat_tui_panels.py:627-655`.
- The TUI worker owns a cancellation token (`cli_impl/tui/app.py:1413-1429`, `:2130-2143`) and soft interrupt clears visible child activity (`:2193-2228`). It does not currently stop the nested runtime.

### 8. Parallelism

- A batch of two or more calls is prelaunched only when every call is a resolved readonly/review subagent, the user has not opted out, no blocking hook is present, and deadline policy permits it (`agent/turn/core.py:679-731`).
- At most four run in a `ThreadPoolExecutor` (`core.py:3537-3570`). Results are consumed in original tool-call order (`:3878-3884`). Write-capable agents remain serialized.

### 9. Result capture and parent handoff

- Final text precedence is the child's authoritative stored `final` event, then a surface completion when no store can be inspected, then latest assistant transcript (`tools_assembly.py:216-264`).
- A missing or non-authoritative final signal is returned as a structured degraded result rather than success (`tools_assembly.py:267-272`, `:1735-1813`).
- Successful JSON includes name, child session ID, result, result source, usage, elapsed time, completed steps, deadline flags, and effective sandbox (`tools_assembly.py:1815-1878`). Intermediate child tool output is not included.
- The parent serializes that object into the tool-result message and must synthesize it (`agent/turn/core.py:4233-4292`). Child usage records are merged into the parent's summary and log (`tools_assembly.py:1238-1251`, `:1608-1614`).

### 10. Error paths, timeouts, and hooks

- Structured errors cover disabled/nested/missing config, missing arguments, unknown/unavailable roles, model selection, deadline launch refusal, child initialization, empty toolset, runtime exception, nonzero exit, and degraded final reports (`tools_assembly.py:1283-1370`, `:1515-1813`).
- Early validation failures return before a subagent start/end event; the outer tool call/result remains the only telemetry.
- Parent deadlines clamp child model/tool timeouts and cause launch refusal during finalization (`execution_deadline.py:175-296`, `:432-603`).
- Post-tool and `SubagentStop` hooks fire after the returned tool result (`agent/turn/core.py:3928-4280`, `hooks/dispatcher.py:263-383`).

## Reproduced bugs

### B01 — Parent cancellation does not stop a child (P0)

**Repro**

1. Start a TUI turn that delegates a slow `implementer` task in `auto` or `fullaccess` mode.
2. Interrupt while the child model/tool is running.
3. Observe that the TUI clears the visible child badge and accepts another turn, but the child worker can finish later; a write issued after the interrupt still reaches the shared workspace.

**Root cause**

- The parent token reaches top-level `AgentSession.run_turn` (`cli_impl/tui/app.py:2130-2143`).
- `_subagent_run` invokes `sub_session.run_turn(task)` without it (`agent/tools_assembly.py:1627`).
- Parallel calls block in `future.result()` and executor shutdown uses `wait=True` (`agent/turn/core.py:3541-3546`, `:3878-3884`).
- TUI comments explicitly account for abandoned child threads finishing late (`cli_impl/tui/app.py:1715-1721`).

**Affected contract**: cancellation, write safety, TUI truthfulness, shutdown latency, end-event cleanup.

### B02 — Read-only one-shot delegation is forced through a mutation gate (P0)

**Repro**

Run the real CLI against the local mock provider with subagents enabled:

```text
Use a subagent to inspect the repository and summarize what it contains.
```

The explorer completes successfully, but the parent makes five provider requests and logs two `empty_diff_finalization_blocked` events before forced acceptance.

**Expected**: a read-only/investigation turn accepts the evidence-backed final after the successful child report; zero repository diff is correct.

**Root cause**: the semantic turn contract already resolves the request as read-only, but the later condition is simply `self.one_shot_execution and workspace_diff.empty`. It ignores that resolved posture.

**Affected contract**: response latency/cost, autonomous research quality, forced-summary behavior, subagent adoption.

### B03 — Streaming child reasoning regression test is stale (P0 release gate)

**Repro**

```powershell
python -m pytest -q tests/test_unknown_tool_recovery.py::test_streaming_subagent_child_session_forwards_reasoning
```

**Actual**: expected `thinking inside child`, observed an empty list.

**Root cause**: `_FakeClient` at `tests/test_unknown_tool_recovery.py:15-46` does not declare a safe streaming reasoning-summary capability. Runtime correctly requires `reasoning_trace_capability.has_safe_summary` and streaming support before supplying a reasoning callback (`agent/turn/core.py:1828-1838`). Adding `ReasoningTraceCapability(output_kind=SUMMARY, supports_streaming=True)` to the fake makes the test pass.

**Affected contract**: full-suite zero-regression gate. Production safety logic should not be weakened.

### B04 — Child loses the parent's active workdir (P1)

**Repro**

1. In a workspace with `packages/a` and `packages/b`, call `session_set_workdir` for `packages/b`.
2. Delegate an explorer task referring to the current package without an explicit path.
3. The new child starts from the workspace context's original focus rather than the parent's current `active_workdir_relpath`.

**Root cause**: `create_session` supports `active_workdir_relpath_override` (`agent/session.py:1420`, `:1482-1493`), and `build_tools` can read the current relpath (`tools_assembly.py:805`, `:1882-1886`), but the child creation call at `tools_assembly.py:1482-1514` does not pass it.

**Affected contract**: monorepo accuracy, relative shell/search behavior, task-context quality.

### B05 — Specialist model routing ignores the review role (P1)

**Repro**: configure different coding and review models/temperatures, run `code-reviewer`, and inspect `subagent_start` telemetry.

**Actual**: coding role/model/temperature.

**Root cause**: all built-ins omit `model_role`; fallback is hard-coded to `ROLE_CODING` (`tools_assembly.py:1261-1281`).

### B06 — Implementer can see the image-generation tool (P1 safety/cost)

**Repro**: enable image generation and launch `implementer`; inspect the returned `sandbox.tools`.

**Expected**: only `visual-designer` can invoke the billable raster tool.

**Actual**: `implementer.allow_tools == ()`, meaning all mode-visible tools, and it has no `image_generate` denial (`subagents.py:142-189`). `frontend-engineer` correctly has that denial (`:190-274`).

### B07 — “Substantive final report” check accepts generic acknowledgements (P1 quality)

**Repro**: a child returns an authoritative final of `Done.` or `OK.`.

**Expected**: degraded/non-substantive result so the parent can retry or report the deficiency.

**Actual**: any non-empty authoritative text passes (`tools_assembly.py:267-272`).

### B08 — Returned report has no instruction-shape safety scan (P1 trust)

Repository files are untrusted and a child report is inserted into the parent tool transcript. The main prompt says to treat it as a report, but no code scans or escapes child text that mimics system/developer/tool tags or permission-setting instructions. Claude Code added such a scan; OpenCode and Kilo do not have one in the pinned comparison. This is a defense-in-depth gap, not evidence of an observed exploit.

## Phase 1 benchmark matrix

This fixed matrix is the Phase 5 before/after benchmark. “Scripted” means the real agent loop with deterministic client responses; “real CLI/mock” means a CLI subprocess using the local OpenAI-compatible server.

| ID | Representative prompt | Harness | Expected | Baseline actual | Baseline |
|---|---|---|---|---|---|
| M01 | `/subagent explorer Read existing README.md and report its contents.` | Real CLI/mock classic chat | One readonly child reads README and returns a content-bearing report in at most two child model calls | Correct lifecycle/tool scope; final mock text only said the tool completed and did not report the contents | **Fail (quality)** |
| M02 | `Use the explorer subagent to inspect the repository and summarize it; do not edit.` | Real CLI/mock one-shot run | Successful child report accepted with no mutation gate | Five provider calls and two empty-diff corrective cycles | **Fail** |
| M03 | `Use implementer to update src/a.py and tests/test_a.py, run focused tests, and report changed files.` | Scripted nested runtime | Auto-mode child can edit only in parent scope, verifies, and returns structured handoff | Mode/tool plumbing passes unit coverage; no shipped nested CLI smoke proves the complete flow | **Coverage gap** |
| M04 | `Use explorer for spawning/context and code-reviewer for failures/cancellation; run them in parallel.` | `test_run_turn_dispatches_same_batch_subagent_runs_in_parallel` | Two readonly children overlap and return in original call order | Passed | **Pass** |
| M05 | `Use debugger to reproduce the failing command and identify the earliest broken invariant; do not edit.` | Built-in prompt/tool contract tests | Diagnostic shell/verify available; write tools absent; evidence-shaped output contract | Tool scope and prompt contract passed | **Pass** |
| M06 | `Use code-reviewer to review this diff with verdict, blocking issues, and test impact.` | Model-resolution inspection + prompt tests | Review model/temperature and readonly tool scope | Readonly scope/prompt pass; model role resolves as coding | **Fail** |
| M07 | `Use explorer to inspect slowly; cancel now.` | Static/runtime cancellation trace; dedicated red test required by PLAN | Child observes parent cancellation, emits one end event, performs no later write | Token never reaches child; TUI only hides late output | **Fail** |
| M08 | `Run visual-designer while image generation is disabled.` | `test_disabled_visual_designer_returns_actionable_capability_error` | Grounded unavailable reason, resolution, no false success | Passed in dirty baseline | **Pass** |
| M09 | `Run a child that returns no authoritative final report.` | `test_subagent_without_final_report_signal_is_degraded` | Structured degraded result with partial text preserved | Passed | **Pass** |
| M10 | `Run explorer near an exhausted deadline, then show the TUI state.` | Deadline tests + TUI subagent suite | Launch refusal with deadline metadata; no stranded badge; result/end rendering correlated | Deadline and TUI suites passed | **Pass** |

Phase 5 must keep these prompts and expectations stable, add executable coverage for M03 and M07, and record both test results and real CLI request/event counts.

## Coverage gaps and audit conclusions

- There is no nested-session case in the shipped credential-free smoke suite.
- There is no cancellation propagation test.
- There is no monorepo active-workdir inheritance test.
- There is no assertion that built-in specialist roles select intended model roles.
- There is no returned-report injection-shape test.
- There is no live-provider response-quality benchmark in this environment; this remains a clearly labeled manual follow-up.
- TUI tests are extensive for rendering and stale-event suppression, but they can pass while a cancelled child continues changing the workspace.

The implementation plan should fix B01-B03 first, then B04-B08, codify M01/M03/M07 in deterministic end-to-end tests, and leave resumable/background/worktree-isolated subagents as deliberately scoped P2 work unless the lower-risk acceptance criteria are already green.
