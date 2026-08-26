# Subagent Orchestration Merge Runbook

This runbook integrates the pre-existing defect fixes before the completed subagent stack and its
evaluation-driven correctness fixes. It is a merge-day procedure only: do not merge or create tags
until every gate below is green. `codex/cache-prefix-stability` is the merge source; it supersedes
`codex/fix-subagent-eval-correctness` and `codex/subagent-worktree-phase2b`, which remain historical
checkpoints only.

## Source checkpoints

The current, pre-integration commit IDs are:

| Checkpoint | Commit |
| --- | --- |
| Current `main` | `852d4efd` |
| Pre-existing defect fixes | `fc166c6f` |
| Phase 1: background scheduler | `471a7ff5` |
| Phase 2a: isolated workspaces | `65dc3aa0` |
| Phase 2b: parallel writers and pinned verification | `974fc624` |
| Phase 3: bounded child helpers | `48682d2c` |
| Phase 4a: steering and dependency chains | `7957efb9` |
| Phase 4b: research, view, and resume | `f320e702` |
| Validation invariant restoration | `332da7ba` |
| Workstream documentation closure | `50784178` |
| Evaluation correctness fixes (`cd66b267..ed49c15f`) | `ed49c15f` |
| Evaluation efficiency fixes (`eaebb753..5e79d75d`) | `5e79d75d` |
| Session-owned preview navigation | `8bee42fe` |
| Post-fix evaluation rerun (`2fb12699..f3a3296c`) | `f3a3296c` |
| Evaluation rerun documentation | `872ccf09` |
| Focused-matrix compatibility (`62ec643a..9606ce14`) | `9606ce14` |
| Cache-prefix analysis and suffix stabilization (`40e01a33..9f557d0a`) | `9f557d0a` |
| Cache-efficiency telemetry | `45d25681` |
| Session-scoped prompt-cache affinity | `6a1c1fab` |
| Third-evaluation run-ID and deadline fixes (`43b201c0..6c2f1e42`) | `6c2f1e42` |
| Third-evaluation apply and execution contracts (`7ae9f8cd..8996d323`) | `8996d323` |
| Child elapsed-time correction | `c30092d1` |
| Third-evaluation documentation | `fd2f2403` |
| Execution-capable test-fixture compatibility | `d365d4a0` |
| Read ledger and keepalive experiment (`b9e858f1..f4d36fb4`) | `f4d36fb4` |
| Fourth-evaluation corrections (`c905765a..a7b91ede`) | `a7b91ede` |
| Current merge source | `codex/cache-prefix-stability` (`HEAD`; latest recorded checkpoint `a7b91ede`) |

If the feature branch is rebased, these IDs become source-history references. Record the rewritten
IDs beside this table before tagging. Merging `main` into the feature branch instead preserves them.

## 1. Land the pre-existing fixes

Start from a clean checkout and refresh the remote refs. Confirm that `main` still points to the
expected integration base or review intervening commits before continuing.

```text
git switch main
git pull --ff-only
git merge --ff-only codex/fix-preexisting-full-suite-defects
ruff check .
pytest -q
```

The full suite must be green apart from genuinely environment-dependent Docker/process cases that
were independently reproduced as such. In particular, the release/VSIX fixture failures must be
gone here; `fc166c6f` includes the package-version-derived fixture fix that removes those 27 stale
`0.9.7` expectations.

Create a recoverable debt-integration checkpoint only after that gate passes:

```text
git tag -a subagents/pre-stack-debt-fixes -m "Pre-subagent debt fixes" main
```

## 2. Integrate the subagent stack

The merge-preserving path keeps the phase checkpoint IDs above stable:

```text
git switch codex/cache-prefix-stability
git merge --no-ff main
ruff check .
pytest -q
pytest -q tests/test_docs_alignment.py tests/test_security_docs.py \
  tests/test_prompt_payload.py
pytest -q tests/test_subagent_benchmark.py
```

A rebase onto updated `main` is also acceptable, but capture the rewritten phase and evaluation
boundary IDs before creating tags. Do not merge from `codex/subagent-worktree-phase2b`; all commits
after `50784178`, including strict mode validation, artifact routing, verification discovery,
diff-first handoffs, exact read continuation, browser verification, and owned-preview navigation,
plus the terminal-event, incomplete-resume, reviewer-economics, Python verification, and sequencing
fixes through `f3a3296c`, exist only on its successor lineage. Resolve only real integration
conflicts; do not carry the feature branch's stale release fixtures over the debt-branch versions.
The cache-prefix analysis, volatile-suffix contract, cache-efficiency telemetry, session-scoped
cache affinity, unified child identity, robust deadline admission, partial-apply acknowledgement,
required execution tools, and child timer fixes through `c30092d1` exist only on the current merge
source. The read ledger, default-off keepalive experiment, continuation-transport refusal,
latency-aware nested admission, resume evidence carryover, structured verification precedence,
honest cancellation partitions, and mechanically bounded reviewer economics through `a7b91ede`
also exist only there.
The full-suite gate should now retain only a documented, reproduced environment-dependent residue.
Any scheduler, batch, isolated-workspace, apply/discard, steering, chain, scout, view, resume, or
browser-preview failure blocks the merge.

## 3. Create phase tags

For a merge-preserving integration, create annotated rollback tags at the source checkpoints:

```text
git tag -a subagents/phase1-background 471a7ff5 -m "Subagents Phase 1"
git tag -a subagents/phase2a-isolation 65dc3aa0 -m "Subagents Phase 2a"
git tag -a subagents/phase2b-parallel-writers 974fc624 -m "Subagents Phase 2b"
git tag -a subagents/phase3-bounded-helpers 48682d2c -m "Subagents Phase 3"
git tag -a subagents/phase4a-steering-chains 7957efb9 -m "Subagents Phase 4a"
git tag -a subagents/phase4b-research-resume f320e702 -m "Subagents Phase 4b"
git tag -a subagents/validation-invariants 332da7ba -m "Subagent validation invariants"
git tag -a subagents/workstream-closed 50784178 -m "Subagent workstream closure"
git tag -a subagents/eval-correctness ed49c15f -m "Subagent evaluation correctness fixes"
git tag -a subagents/eval-efficiency 5e79d75d -m "Subagent evaluation efficiency fixes"
git tag -a subagents/preview-navigation 8bee42fe -m "Session-owned preview navigation"
git tag -a subagents/eval-rerun-fixes f3a3296c -m "Post-fix evaluation rerun fixes"
git tag -a subagents/eval-rerun-docs 872ccf09 -m "Evaluation rerun documentation"
git tag -a subagents/eval-matrix-fix 9606ce14 -m "Evaluation focused-matrix compatibility"
git tag -a subagents/cache-suffix-stability 9f557d0a -m "Stable provider prompt suffix contract"
git tag -a subagents/cache-affinity 6a1c1fab -m "Session-scoped prompt cache affinity"
git tag -a subagents/eval-third-rerun c30092d1 -m "Third evaluation correctness fixes"
git tag -a subagents/read-ledger-keepalive f4d36fb4 -m "Read ledger and keepalive experiment"
git tag -a subagents/eval-fourth-rerun a7b91ede -m "Fourth evaluation correctness fixes"
git tag -a subagents/stack-validated HEAD -m "Validated subagent orchestration stack"
```

For a rebase, substitute the recorded rewritten IDs. Inspect every tag with `git show --no-patch`
before pushing; push tags only after the final full-suite gate and maintainer review.

## 4. Apply the release checklist

Use [`docs/release_checklist.md`](../release_checklist.md) in full, with particular attention to:

- autonomous and explicit subagent step-limit behavior;
- fresh, host-observed verification and mutation evidence before candidate apply;
- public compatibility for `run_agent`, `AgentSession.run_turn`, `create_session`, result keys,
  lifecycle events, and the `_patchable()` facade;
- cancellation, finalization-window behavior, and truthful incomplete/deadline results;
- the invocation-wide monotonic deadline: every child inherits the parent's absolute deadline,
  never receives a fresh extension, and cannot launch inside the finalization window;
- focused deadline regressions in `tests/test_subagents.py`, `tests/test_config.py`, and
  `tests/test_session_store.py`, followed by the full-suite gate;
- sanitized diagnostics and no prompts, tool arguments, source, commands, or secrets in telemetry.

Also confirm both prompt-payload tests retain at least 120 estimated tokens of headroom and that
Forge execution and swarm workers still expose no new orchestration tools.

## 5. Rollback

Never rewrite a published integration branch. If the stack has not reached `main`, reset the merge
candidate by creating a new branch from `subagents/pre-stack-debt-fixes`; keep the failed branch and
artifacts for diagnosis. If it has reached `main`, revert the merge commit, rerun the debt-branch
full-suite gate, and publish a patch release.

For a regression after the feature phases, use the latest preceding tag as the known-good diagnostic
point: fourth-rerun fixes roll back to `subagents/read-ledger-keepalive`; read-ledger/keepalive
changes roll back to `subagents/eval-third-rerun`; third-rerun fixes roll back to
`subagents/cache-affinity`; cache affinity rolls back to
`subagents/cache-suffix-stability`; cache suffix stabilization rolls back to
`subagents/eval-matrix-fix`; the focused-matrix fix rolls back to `subagents/eval-rerun-docs`; evaluation-rerun
documentation rolls back to `subagents/eval-rerun-fixes`; evaluation-rerun fixes roll back to
`subagents/preview-navigation`; preview navigation rolls back to
`subagents/eval-efficiency`; evaluation efficiency to `subagents/eval-correctness`; evaluation
correctness to `subagents/workstream-closed`; and workstream closure to
`subagents/validation-invariants`; validation invariants to `subagents/phase4b-research-resume`.
For a phase regression, Phase 4b rolls back to
`subagents/phase4a-steering-chains`, Phase 4a to `subagents/phase3-bounded-helpers`, Phase 3 to
`subagents/phase2b-parallel-writers`, Phase 2b to `subagents/phase2a-isolation`, Phase 2a to
`subagents/phase1-background`, and Phase 1 to `subagents/pre-stack-debt-fixes`. Prefer reverting the
offending commits on a new branch over moving or deleting tags. After any rollback, rerun Ruff, the
focused browser/subagent tests, prompt-payload tests, and the full suite before release.
