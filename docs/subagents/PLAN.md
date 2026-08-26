# Subagent improvement plan

Date: 2026-07-17

This plan turns [AUDIT.md](AUDIT.md) and [COMPARISON.md](COMPARISON.md) into executable acceptance criteria. P0 and P1 are in scope for this implementation session. P2 items are explicitly gated capabilities, not permission to add large orchestration systems without their prerequisites.

## Delivery rules

- One bounded subagent assignment per P0/P1 item or tightly coupled pair.
- Every behavior change adds or extends a regression test that fails for the baseline reason.
- The primary agent reviews every diff for scope, runs the focused tests, and rejects unrelated edits.
- Commits stay focused: runtime safety, completion behavior, timeout/config, context/tool routing, report safety, capability/TUI integration, and benchmark coverage do not share a catch-all commit.
- The existing TUI surface/event contract remains intact. New fields may be additive; existing event names and required payloads are not silently renamed.
- Pre-existing dirty changes remain user-owned until reviewed against an item below. They are never swept into a commit merely to make the tree clean.
- A fix does not count until its focused tests and the Phase 5 full suite pass.

## P0 — Current behavior is unsafe, incorrect, or blocks release

### P0.1 — Propagate cancellation through every subagent path

**Problem**: the parent turn token never reaches `sub_session.run_turn`, so a soft-interrupted child can outlive the turn and continue writing.

**Implementation boundary**

- Carry the active parent cancellation token into `subagent_run` dispatch without exposing a model-settable schema field.
- Pass it to child `run_turn` for both serial and parallel tool dispatch.
- Make lifecycle cleanup cancellation-safe: close the child, replay already-recorded usage once, and emit exactly one terminal subagent event/result.
- Avoid a parallel executor shutdown path that waits indefinitely after cancellation.
- Preserve direct classic-chat `/subagent` behavior when no parent token exists.

**Acceptance criteria**

1. A blocking fake child receives the exact parent token object.
2. Cancelling the token causes the parent turn to return within a bounded test interval and no child mutation occurs after cancellation.
3. Serial and same-batch parallel subagent tests both cover cancellation.
4. The parent log and surface contain one terminal end event per started child, classified as cancelled/failed with a machine-readable cancellation category; no success is emitted.
5. Existing parallel ordering, usage replay, and TUI stale-event tests still pass.

**Focused verification**

```powershell
python -m pytest -q tests/test_subagent_cancellation.py tests/test_agent_loop_event_emission.py tests/test_subagents.py -k "cancel or parallel or usage"
```

### P0.2 — Let read-only one-shot work finish without a diff

**Problem**: `alysis run` unconditionally applies the empty-diff repair gate, even when the semantic turn contract requests inspection/read-only work and a research child returned the requested answer.

**Implementation boundary**

- Apply empty-diff and no-material-edit finalization guards only to mutating execution intent.
- Do not weaken the gate for prompts that ask to implement, fix, create, update, or otherwise change the repository.
- Preserve explicit subagent-use enforcement and parent synthesis.

**Acceptance criteria**

1. A one-shot inspection prompt that successfully calls explorer returns after synthesis with no `empty_diff_finalization_blocked` or `empty_diff_forced` event.
2. The real CLI/local-mock M02 case completes without an empty-diff corrective cycle.
3. Existing mutating empty-diff tests still prove that a “fix” answer with no diff is rejected.
4. Plan-only and advisory classification tests remain unchanged.

**Focused verification**

```powershell
python -m pytest -q tests/test_agent_loop_one_shot_follow_through.py tests/test_subagent_turn_policy.py -k "empty_diff or readonly or subagent"
```

### P0.3 — Restore a clean release gate and add a real nested smoke

**Problem**: one deterministic streaming test fake lacks required safe-summary metadata, and shipped credential-free CLI smoke coverage disables subagents.

**Implementation boundary**

- Update the fake client capability declaration; do not relax production reasoning-safety checks.
- Add a real CLI subprocess smoke using `MockLLMServer`, explicit classic-chat `/subagent explorer`, and an isolated config/data/workspace.

**Acceptance criteria**

1. `tests/test_unknown_tool_recovery.py` passes without changing the runtime safe-summary predicate.
2. The smoke proves a child session is created, receives readonly tools, performs `fs_read`, returns through the CLI result panel, and exits 0.
3. The smoke asserts no write occurred and no raw traceback/`LLMError` leaked.
4. The test is deterministic and makes no external network call.

**Focused verification**

```powershell
python -m pytest -q tests/test_unknown_tool_recovery.py tests/test_mock_provider_smoke.py -k "subagent or reasoning"
```

### P0.4 — Give ordinary subagents a bounded fallback wall clock

**Problem**: when the parent has no deadline, an unlimited-default child can run forever.

**Implementation boundary**

- Add a documented ordinary-subagent timeout setting with a conservative finite default.
- Derive the child deadline as the earlier of the parent absolute deadline and the child fallback; reuse the parent object when it is already earlier.
- Clamp child provider/tool calls through the existing `ExecutionDeadline` machinery; do not add an independent kill mechanism.
- Expose the resolved timeout/deadline source in start/end telemetry.

**Acceptance criteria**

1. Without a parent deadline, a child receives a finite deadline at the configured duration.
2. With an earlier parent deadline, the exact parent deadline object remains the ceiling.
3. With a later parent deadline, the child receives the earlier derived deadline and cannot extend the parent run.
4. Invalid zero/negative config is rejected; config serialization/menu compatibility tests pass.
5. Existing deadline launch-refusal and timeout-clamping tests pass.

**Focused verification**

```powershell
python -m pytest -q tests/test_subagents.py tests/test_execution_deadline.py tests/test_config.py -k "subagent and deadline or subagent_timeout"
```

## P1 — Response quality, context fidelity, and trust

### P1.1 — Inherit active workdir and correlate child identity

**Problem**: a delegated monorepo task starts from the original workspace focus, and the start event is emitted before the child session ID is known.

**Implementation boundary**

- Pass the parent's current `active_workdir_relpath` into child session creation.
- Reject an invalid/nonexistent inherited focus through the existing session workdir error path.
- Add a correlation event/update after child creation or move the public start event to the point where the child ID is known, without breaking surface ordering.

**Acceptance criteria**

1. After `session_set_workdir packages/b`, child prompt/store/tool runtime all report `packages/b` as active workdir.
2. Relative child shell/search behavior is rooted at that focus while repository-root-relative file tools remain safe.
3. Every successfully created child has one start record that can be joined to its end record by non-empty child session ID.
4. Parallel same-name children remain independently correlatable.
5. Existing TUI “start before nested tool” ordering remains true.

**Focused verification**

```powershell
python -m pytest -q tests/test_subagents.py tests/test_chat_session_workdir.py tests/test_tui_subagent.py -k "subagent or active_workdir"
```

### P1.2 — Tighten specialist model and tool scoping

**Problem**: `code-reviewer` uses the coding model role, implementer can see `image_generate`, and unavailable custom allowlist entries can degrade into a confusing empty/partial tool surface.

**Implementation boundary**

- Set `code-reviewer` to the review model role; retain explicit per-definition model override precedence.
- Deny `image_generate` to general implementer, preserving `visual-designer` as the only built-in raster role.
- If a non-empty custom allowlist has entries unavailable in the resolved child session, fail launch with the unresolved names and effective mode instead of silently dropping them. `subagent_run` remains a special always-removed recursion entry.

**Acceptance criteria**

1. Different configured coding/review models cause code-reviewer telemetry and client construction to use the review model and review temperature.
2. Explorer/debugger/test-strategist behavior is unchanged unless explicitly planned.
3. Implementer sandbox never includes `image_generate`, even when image generation is enabled.
4. A custom `tools: [fs_reed]` definition fails with `unavailable_allowed_tools: ["fs_reed"]`; it does not call a model.
5. Valid custom allowlists and Claude-style aliases continue to work.

**Focused verification**

```powershell
python -m pytest -q tests/test_subagents.py tests/test_tool_registry.py tests/test_config.py -k "model_role or image_generate or allowlist"
```

### P1.3 — Reject useless finals and neutralize instruction-shaped reports

**Problem**: an authoritative `Done.` counts as substantive, and returned child text enters the parent transcript without an instruction-shape scan.

**Implementation boundary**

- Conservatively classify generic acknowledgements (`Done`, `OK`, `Completed` with no evidence) as non-substantive; do not reject short factual answers such as `42` or `False`.
- Before parent ingestion, scan role/harness tags and high-risk permission/instruction-override phrases.
- Escape instruction-shaped tags in the returned parent-facing result and attach bounded safety metadata. Preserve raw child text only in the child/session audit log.
- Strengthen the parent system contract to treat child reports as untrusted evidence, never instructions.

**Acceptance criteria**

1. Authoritative `Done.` returns degraded `non_substantive_final_report`; `42` remains successful.
2. `<system>`, `<developer>`, `<tool>`, and Alysis Code harness/context tags are escaped in parent-facing text and listed in safety metadata.
3. Benign code snippets and ordinary findings are byte-for-byte unchanged.
4. Raw suspicious text is not present in the parent's serialized tool-result message.
5. Parent synthesis tests prove that a report cannot change permission mode or demand an unrelated tool call.

**Focused verification**

```powershell
python -m pytest -q tests/test_subagents.py tests/test_agent_loop_tool_transcript_shaping.py tests/test_prompt_payload.py -k "report or injection or substantive"
```

### P1.4 — Review and integrate the pre-existing capability/TUI patch

**Problem**: the starting tree contains uncommitted changes for capability-gated visual delegation, semantic routing, explicit-command errors, and TUI identity. They are relevant but not yet a verified phase artifact.

**Implementation boundary**

- Review every pre-existing hunk for consistency with the lifecycle contract and avoid absorbing unrelated visual changes by accident.
- Keep image generation hidden from callable roles when unavailable while returning a grounded reason/resolution for direct requests.
- Ensure a raster request cannot be answered with prompt text in place of a generated artifact.
- Preserve the agreed name-based TUI identity and one-shot explicit command behavior only if all focused tests pass.

**Acceptance criteria**

1. Disabled image generation omits `visual-designer` from the callable enum but exposes its unavailable reason and setup instructions in status/context.
2. Enabled image generation exposes exactly `visual-designer` plus `image_generate` in its sandbox.
3. Semantic routing sends an artifact request to the repository agent whether capability status is available or unavailable; the result is either a tool-grounded artifact or a grounded blocker.
4. Nested `/subagent` slash text is rejected before any child starts.
5. Classic CLI and TUI tests pass; no glyph/encoding regression appears in snapshots.

**Focused verification**

```powershell
python -m pytest -q tests/test_subagents.py tests/test_cli_subagents.py tests/test_tui_subagent.py tests/test_prompt_payload.py tests/test_language_policy_prompts.py
```

### P1.5 — Make the Phase 1 matrix executable

**Problem**: M03 and M07 lack executable end-to-end coverage, and M01 only exists as an ad hoc subprocess run.

**Implementation boundary**

- Add fixed deterministic cases for explicit explorer lookup, implementer multi-file edit/verification, parallel readonly decomposition, cancellation, unavailable capability, empty final, deadline refusal, and TUI result attribution.
- Reuse the repository mock provider and temporary isolated workspaces; do not create a second fake protocol stack.

**Acceptance criteria**

1. All M01-M10 cases map to named test nodes or one documented real-CLI command.
2. The benchmark records provider request count, child role, tool names, terminal status, and changed paths where applicable.
3. No case uses a live credential or internet.
4. The benchmark can be rerun independently in under two minutes on the reference machine.

## P2 — New capabilities, gated by safety prerequisites

These items are recommendations for the next iteration. They are not complete merely because a schema field or UI label exists.

### P2.1 — Resumable child sessions

**Acceptance criteria before release**

1. Success and failure results return an opaque child ID.
2. Resume verifies child ownership against the calling parent/session; cross-parent IDs fail closed.
3. Resume refreshes the current parent mode, tool denies, workspace confinement, and deadline rather than reusing stale authority.
4. Usage deltas are merged exactly once across create/resume/abort.
5. Missing IDs return not-found and never create an accidental new child.
6. Persistence, migration, cancellation, and transcript-retention tests pass on Windows and POSIX.

### P2.2 — Optional worktree isolation for write-capable children

**Acceptance criteria before release**

1. A write child receives a verified temporary worktree and cannot resolve shell/file writes into the main checkout.
2. The parent gets an explicit patch/commit artifact and chooses whether to apply it.
3. Cleanup is recoverable after crash/cancel; unmerged child work is never silently deleted.
4. Only after these pass may two write-capable children execute in parallel.

### P2.3 — Durable background jobs

**Acceptance criteria before release**

1. Start returns a stable job/child ID and the parent remains usable.
2. Completion, failure, permission request, and cancellation are durably delivered exactly once.
3. Parent shutdown recursively cancels or explicitly detaches jobs according to a visible policy.
4. Background writes require P2.2 isolation.
5. TUI/CLI can inspect status and last output without loading the full child transcript into the parent context.

### P2.4 — Per-agent completion hooks

**Acceptance criteria before release**

1. A definition can name bounded local validation hooks without embedding shell strings in model output.
2. Hooks run under the child permission/workspace ceiling and can reject completion with a structured reason.
3. Hook output is size-bounded, logged, and treated as untrusted evidence.
4. Missing/invalid hook declarations fail at definition validation, before model invocation.

## Phase 5 release gate

Implementation is complete only when all of the following are true:

1. M01-M10 are rerun with before/after results recorded in `FINAL_REPORT.md`.
2. Every focused P0/P1 command passes.
3. `python -m pytest -q` passes with zero failures.
4. `ruff check src tests scripts` and `ruff format --check src tests scripts` pass for touched Python files or any repository-wide pre-existing failures are isolated and documented without being hidden.
5. `git diff --check` passes.
6. Each implementation commit is listed with its acceptance criteria and verification evidence.
