# Forge

Forge is Alysis Code's plan-driven workflow for larger coding tasks. It turns a broad request into
an explicit task plan, executes scoped tasks, and keeps verification and review evidence visible.

Use it for multi-file implementation work, staged refactors, release cleanup, or any task where you
want an explicit plan before changes are made.

## Start From Chat

From a repository workspace:

```bash
alysis chat
/forge
```

Inside Forge, use the on-screen plan commands to refine the goal, inspect tasks, edit the plan,
and execute approved work. `/back` returns to normal chat while preserving the current run pointer
for the same workspace.

Use `/forge resume` when you want to attach explicitly to the current run pointer instead of
starting a fresh run for the chat session.

## Direct CLI Flow

Create or open a plan:

```bash
alysis forge plan --path .
alysis forge show --path .
alysis forge status --path .
```

Execute the whole plan — this is the default way to run a plan:

```bash
alysis forge run --path . --mode auto --verify strict
```

Preview the execution order without running anything:

```bash
alysis forge run --path . --dry-run
```

Execute one task from the plan:

```bash
alysis forge exec T01 --path . --mode review
```

Run a PR-style local flow for one task:

```bash
alysis forge exec T01 --path . --pr --verify strict --review
```

Run independent tasks in parallel (advanced — see [Swarm Runs](#swarm-runs)):

```bash
alysis forge swarm --path . --parallel 3 --mode auto --verify warn
```

Review a task result:

```bash
alysis forge review T01 --path .
```

Pick up a run that was interrupted, or inspect the lock a dead run left behind (see
[Interrupted Runs](#interrupted-runs-status-forge-unlock-forge-resume)):

```bash
alysis forge resume --path .
alysis forge unlock --path .
```

## Machine Mode (`--machine`)

`forge plan`, `show`, `status`, `review`, `exec`, `run`, `resume`, `unlock`, `swarm`, and
`attach` all accept
`--machine`. Without it nothing changes: Forge prints the same human output it always has,
and the interactive TUI is unaffected. With it, the command suppresses that output and
writes **newline-delimited JSON to stdout instead** — one JSON object per line, nothing
else. Tools should parse these events rather than scraping prose like `Plan saved: ...`,
which is not a stable interface.

(The `forge assets` subcommands are not part of this protocol yet and keep printing human
output.)

The flag works in both positions:

```bash
alysis forge plan --path . --machine
```

```bash
alysis forge --machine plan --path .
```

### Event Envelope

```json
{"v": 1, "event": "plan_saved", "ts": "2026-01-01T00:00:00+00:00", "run_id": "017-wayland", "data": {}}
```

- `v` is the schema version. Adding a field inside `data` is a compatible change;
  renaming an event or changing the envelope bumps `v`.
- `run_id` is `null` until the run is resolved (usually only on `run_started`).
- `ts` is an ISO 8601 UTC timestamp.

### Events

| Event | Meaning |
| --- | --- |
| `run_started` | First event of every invocation; `data` echoes the command and its arguments. |
| `plan_saved` | The plan was written. Carries paths, task ids, validation warnings, `execution_ready`, and the plan-status fields below. |
| `plan_invalid` | The plan cannot be executed as-is (no runnable tasks, unknown task id, unmet dependencies, failed validation). Carries the same plan-status fields. |
| `task_started` | A task moved to `in_progress`. |
| `task_completed` | A task reached `done`, `completed_unverified`, or was accepted as already satisfied. |
| `task_failed` | A task ended in `failed`, `verify_failed`, `changes_requested`, `merge_conflict`, or `interrupted`. |
| `scope_amended` | Strict scope triage accepted adjacent changes and widened the task's `write_scope`. Carries `added_patterns`, the per-path `amendments`, and `adjacent_only`. |
| `verification_result` | Verification ran. Carries `passed`, the commands, the artifact path, the structured result, and `pass` (`initial` or `repair.<n>`). |
| `verification_unavailable` | Verification did not run, with the reason and whether that blocked the task. When the task was kept anyway it also carries `outcome: "completed_unverified"`. |
| `review_result` | A review completed. Carries `approved`, confidence, issue counts, and artifact paths. |
| `run_completed` | Terminal. The command ran to completion; `data.ok` says whether the work was accepted. |
| `error` | Terminal. The command itself failed. |

### Plan Status and Repair Fields

`plan_saved`, `plan_invalid`, and `forge show` all carry the same five fields, so a
consumer never has to re-derive execution readiness from the plan JSON:

| Field | Meaning |
| --- | --- |
| `plan_status` | `execution_ready` or `draft`. A draft is a plan `forge exec` would reject. |
| `plan_status_blocking_reasons` | The acceptance-rule failures (`R1`-`R5`) behind a `draft`. Empty when execution-ready. |
| `host_repaired` | The planner never produced a valid payload, so the host repaired one. |
| `host_repaired_fields` | Dotted paths the host had to change, e.g. `plan_update.tasks_add[0].write_scope`. |
| `forced_draft` | The planner hit the clarification-round cap and was made to draft a plan. |

`plan_status` is decided by the execution gate's own rule, so the two can never disagree:
a plan that says `execution_ready` is one `forge exec` accepts. `validate_plan` warnings
(a missing `acceptance_criteria`, say) are recorded in the plan's `plan_status_detail` but
are advisory — they do not make a plan a draft, because the gate does not block on them.

### Planner Payload Repair

Planner responses are validated strictly. A payload that fails validation, or that would
produce a plan the execution gate rejects, is sent back to the model with the exact errors
and a blunter instruction each round. Host-side repair runs only after that budget is
spent, and when it runs the fields it touched are recorded on the plan and reported in the
events above.

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALYSIS_PLAN_REPAIR` | on | Kill-switch. Off restores the previous single-retry behaviour. |
| `ALYSIS_PLAN_REPAIR_ATTEMPTS` | 3 | Total payload attempts per planner turn (capped at 6). |
| `ALYSIS_PLAN_REPAIR_CLARIFICATION_ROUNDS` | 2 | Clarification-only turns allowed for one goal before a draft plan is forced (capped at 6). |

### The Terminal-Event Invariant

**Every invocation emits exactly one terminal event — `run_completed` or `error` — before
the process exits.** This holds on success, on handled failure, on an unhandled exception,
and on Ctrl-C. Events emitted after the terminal one are dropped rather than appended, so a
consumer can treat the first terminal event as the end of the stream.

A consumer that reads to EOF without seeing a terminal event should treat the job as
crashed or killed, not as still running.

### Exit Codes

`--machine` does not change exit codes; it makes them legible. The contract is:

| Code | Meaning |
| --- | --- |
| `0` | The command did its job. |
| `1` | The command ran to completion, but the work was not accepted (task failed, verification blocked, swarm left tasks unfinished). |
| `2` | The command itself failed (bad config, missing run, unhandled exception). |

Two conflations were removed to make this true:

- **`forge review` now exits 0 even when it rejects the work.** A review that says "changes
  requested" is a review that did its job; the verdict lives in `review_result.approved` and
  in `run_completed.data.review.approved`. Scripts that used `alysis forge review ... ;
  echo $?` to detect rejection must read the approval flag instead.
- **`forge swarm` now exits 2 on an unexpected exception** instead of 1, so exit 1 means only
  "the swarm ran and some tasks were not accepted".

`forge exec` is unchanged: a task that ran and was not accepted is a genuine execution
failure, so it still exits 1, with exit 2 reserved for the command failing.

### Example

```bash
alysis forge exec T01 --path . --machine
```

```
{"v":1,"event":"run_started","ts":"...","run_id":null,"data":{"command":"forge.exec","task_id":"T01","pr":false,"review":false,"verify":"warn"}}
{"v":1,"event":"task_started","ts":"...","run_id":"017-wayland","data":{"task_id":"T01","status":"in_progress"}}
{"v":1,"event":"verification_result","ts":"...","run_id":"017-wayland","data":{"task_id":"T01","passed":true,"policy":"warn"}}
{"v":1,"event":"task_completed","ts":"...","run_id":"017-wayland","data":{"task_id":"T01","status":"done"}}
{"v":1,"event":"run_completed","ts":"...","run_id":"017-wayland","data":{"command":"forge.exec","ok":true,"exit_code":0}}
```

`forge plan --machine` still reads planning commands from stdin; its `plan` prompt is written
to **stderr** so stdout stays pure NDJSON. Drive it by piping the commands in:

```bash
printf '/goal Ship the parser\n/task Implement src/parser.py\n/done\n' | alysis forge plan --path . --machine
```

## Plan Artifacts

Forge stores run state under the workspace's Alysis Code runtime directory. The important artifacts
are the structured task plan, a human-readable plan summary, per-task execution logs, verification
results, and review outputs.

Tasks should stay small and scoped. A good task has:

- a clear objective
- explicit write paths
- verification commands or acceptance criteria
- dependencies on earlier tasks when needed

## Executing A Plan: `forge run`

`forge run` is the blessed execution path. It takes every ready task, in dependency order, one
at a time, **in the main checkout**:

```bash
alysis forge run --path . --mode auto --verify strict --yes
```

What it deliberately does *not* do:

- no per-task git worktrees,
- no parallel workers,
- no batch integration gate,
- no LLM merge-conflict resolver agent.

Each task is a branch → execute → commit → verify → (review) → merge cycle in the checkout you
are sitting in, and the next task starts from the merged result. That is what makes per-task
verification meaningful: a task is checked against the integrated tree, not against an isolated
worktree that has not met the other tasks yet.

Because there is no parallelism there is also one linear narrative. Every step is appended to
`.alysis/runs/<run_id>/execution/sequential_run.jsonl`, and the run outcome is written to
`sequential_summary.json` next to it. With `--machine` the same run produces one ordered NDJSON
event stream (same events as every other Forge command).

Useful controls:

- `--dry-run` prints the execution order and exits. The order is simulated task by task, so it
  reflects dependencies unlocking as tasks finish.
- `--only T01,T02` restricts the run; dependencies are still enforced.
- `--max-tasks <n>` stops after n tasks.
- `--keep-going` continues with the next independent task after a failure. **The default is to
  stop**, because a failed task usually leaves everything downstream of it unsound.
- `--retry-failed` / `--retry-changes-requested` include tasks that were previously not accepted.
  A task is still attempted at most once per run, so a task that fails again does not loop.
- `--no-pr` executes in place without branches, commits or the verification gate. Use it only
  where git flow is unavailable; a workspace with no git HEAD degrades to it automatically with
  a warning and a `verification_unavailable` event.
- `--scope`, `--verify`, `--verify-cmd`, `--verify-repair-attempts`, `--review` behave exactly
  as they do for `forge exec` — it is the same execution core.

Exit codes follow the usual contract: `0` when every executed task was accepted, `1` when a task
ran and was not accepted, `2` when the command itself failed. Tasks left unexecuted because of
`--only` or `--max-tasks` are a scope you chose, not rejected work, and do not change the exit
code.

## Interrupted Runs: Status, `forge unlock`, `forge resume`

A run that is killed, crashes, or loses its terminal leaves two things behind: the workspace
lock its process was holding, and a run pointer that still says the run is going. Both recover
on their own; the commands below exist for the cases where they cannot.

### The Run Lifecycle

`.alysis/current_run.json` carries an explicit `status` (`schema_version` 5):

| Status | Meaning |
| --- | --- |
| `draft` | The plan is not execution-ready yet. |
| `approved` | The plan passes the same gate `forge exec` applies. Executable, nothing running. |
| `running` | A process is executing this plan right now. `run_owner` says which pid, on which host. |
| `interrupted` | Execution stopped without finishing. Resumable. |
| `completed` | Execution finished and the work was accepted. |
| `failed` | Execution finished and the work was not accepted. |

`forge status` prints it, and every `--machine` payload from `forge show`/`forge status` carries
`run_status`, `run_status_reason`, `run_owner`, and `resumable`. Pointers written by older builds
have no status and read as `draft`; they claim nothing about work this build never saw.

**Crash leftovers heal on read.** A run marked `running` holds the workspace mutation lock for
its whole life, so `running` with no live lock describes a process that is not there. Any command
that opens the run — `forge status`, `forge show`, `forge run`, `forge resume` — moves it to
`interrupted` first. No UI has to patch `current_run.json` itself to un-stick a workspace.

### Lock Recovery And `forge unlock`

The lock records two independent liveness signals, and either one is enough to conclude the owner
is gone:

- **pid** — cleared instantly by `kill -9`, but it lies when the id has been recycled.
- **heartbeat** — a timestamp the owner refreshes every `heartbeat_interval_s` (default 15s) and
  promises to keep fresher than `heartbeat_ttl_s` (default 120s). A recycled pid cannot forge it.

A **same-host** lock is recovered automatically when its pid is gone *or* its heartbeat has
lapsed. A heartbeat-expired lock is re-read after one grace interval first, so a machine that
merely came back from sleep gets to prove it is alive. Locks written before the heartbeat contract
existed are never reaped by age: they never promised to beat, so their silence proves nothing.

Everything else still fails closed. A lock held by another host is ambiguous by construction — this
machine cannot probe that host's process table — so it blocks, and the error names the lock's age,
owner, host, pid, and the command that clears it.

```bash
alysis forge unlock --path .            # inspect; clear what is provably stale
alysis forge unlock --path . --force    # clear anyway (you are asserting the owner is dead)
alysis forge unlock --path . --machine  # same, as NDJSON
```

`forge unlock` reports each lock's verdict (`stale`, `active`, `ambiguous`) with the reason in
words, clears the stale ones, and reconciles the run status afterwards. Exit `0` when the
workspace ends up unlocked, `1` when a lock was kept because its owner could still be alive.
`--force` is for the genuinely ambiguous case only: if that process is in fact alive, two
executions can now mutate the workspace at once.

Environment overrides: `ALYSIS_RUN_LOCK_HEARTBEAT=0` disables the heartbeat thread,
`ALYSIS_RUN_LOCK_HEARTBEAT_INTERVAL_S` and `ALYSIS_RUN_LOCK_TTL_S` tune the contract. The
TTL is floored at twice the interval so a healthy owner is never reaped between beats.

### `forge resume`

```bash
alysis forge resume --path .
```

Resume picks an `interrupted` (or `failed`) run back up from its last incomplete task:

1. **Revalidates the plan.** It recomputes the plan's fingerprint — goal, summary, requirements,
   and each task's title, description, acceptance criteria, dependencies, files and write scope —
   and compares it to the fingerprint recorded when the run started. Task *status*, *attempts* and
   *branch* are excluded, so a run's own progress never reads as an edit.
2. **Surfaces drift instead of guessing.** If the plan moved while nothing was running, resume
   stops and lists exactly what moved (`project goal changed`, `T02 changed: acceptance criteria`,
   `tasks added: T04`). It does **not** silently reset the run to `draft` — you have not rejected
   the plan, you have not looked at it yet. Pass `--reapprove` to accept the current plan and
   continue; the run re-records its fingerprint at that point.
3. **Re-arms tasks the dead process left mid-flight.** A task stuck at `in_progress` is not
   runnable by design, so resume rewrites it to `interrupted` — which is runnable, and which
   records what happened rather than pretending the task was never started.
4. **Continues.** Execution is `forge run` with retries enabled for the statuses an interruption
   leaves behind, so it accepts every `forge run` flag (`--verify`, `--scope`, `--only`, `--pr`,
   `--keep-going`, …). `--dry-run` reports what would be resumed and exits.

Refusals are explicit: a `completed` run says start a new one, an `approved` run says use
`forge run`, a `draft` run says finish planning, and a `running` run whose lock is still live says
wait or inspect the lock.

### Merge Conflicts Stop The Run

If a task branch does not merge, `forge run` and `forge exec --pr` **stop and report**. They do
not start a resolver agent. The report names the branches, points at the conflict review
artifact, and gives you the commands to land it by hand and resume:

```bash
git checkout <base>
git merge --no-ff <task-branch>
# resolve, then commit
alysis forge run --path .        # continues with the remaining tasks
```

Agent-driven conflict resolution runs a second agent inside a dedicated worktree. That is swarm
machinery, and it is opt-in on the sequential path:

```bash
alysis forge exec T01 --path . --pr --auto-resolve-conflicts
```

`forge swarm` is unchanged: it still auto-resolves conflicts by default, because parallel task
branches genuinely conflict with each other and stopping the whole swarm on the first one is not
a useful default there.

## Execution And Review

`forge exec` runs a single task — the same core `forge run` calls per task, so everything in this
section applies to both. By default, write-scope enforcement is strict. Use `--scope warn`
or `--scope off` only when a task legitimately needs broader edits.

`--pr` creates a local PR-style flow around the task: branch, execute, commit, verify, review, and
merge back when the gates pass. `--keep-branch` keeps the task branch for debugging.

`--verify` controls verification policy:

- `off`: do not run the verification gate
- `warn`: collect verification output without hard-failing the flow
- `strict`: fail the task when verification fails

Repeat `--verify-cmd` to provide explicit verification commands.

### Verification Outcomes

Verification failing and verification being *unavailable* are different facts, and Forge no
longer reports both as "task failed".

| Situation | Outcome |
| --- | --- |
| Verification ran and passed | `done`, merged in `--pr` mode. |
| Verification ran and failed | Up to `--verify-repair-attempts` repair attempts (below). If it still fails under `--verify strict`: `verify_failed`, not merged. Under `--verify warn`: recorded as a warning, still merged. |
| No authoritative verification command exists | `completed_unverified` under `--verify strict`. Under `--verify warn` and `--verify off`, unchanged: the task is `done`. |

`completed_unverified` means the work landed and was kept — nothing checked it. It is a
completion, not a failure: exit code `0`, a `task_completed` event, and the task counts as
finished for dependent tasks. Nothing is merged either way:

- `forge exec --pr`: the commit stays on the task branch, the branch is never deleted, and the
  working tree is returned to the base branch so a human can review before merging.
- `forge swarm`: the task is marked `completed_unverified`, its branch is not merged, and its
  worktree is **not** cleaned up — the work stays where you can review it.

`verify_gate` deliberately suppresses generic fallbacks and empties the command list for
docs-only, static-web, CI-only, and Terraform/Compose workspaces, so this is the normal outcome
there — and missing tooling never discards written files. Pass `--verify-cmd` when you do have a
command you want held against the work.

### Verification Repair

When strict verification **runs and fails**, the failing command's own output is fed back to
the same executing agent, which gets a bounded number of attempts to fix it before the task is
failed. After each attempt the repair is committed to the task branch and verification is
re-run.

- `--verify-repair-attempts <n>` sets the budget (default `2`, `0` disables it, capped at `10`).
  `ALYSIS_VERIFY_REPAIR_ATTEMPTS` sets the same budget for a whole session; the flag wins.
- Repair runs only under `--verify strict`, the only mode where a failing gate blocks, and only
  in the `forge exec --pr` flow. `forge swarm` workers do not repair in-run; a swarm task whose
  verification failed is retried with `forge swarm --retry-failed`.
- The repair prompt repeats the task's own instruction (each attempt is a fresh session) and
  appends the failing commands with the *tail* of their output — the part carrying the
  assertion, not the collection noise.
- The loop stops early when verification passes, when an attempt changes nothing (re-running
  the same commands would produce the same failure), when the repair agent exits non-zero, or
  when the failure is infrastructure-unavailable rather than something code can fix.
- Every attempt is recorded in the task report, in the `task_completed`/`task_failed` event
  under `verification_repair`, and as `verification_result` events labelled `repair.<n>`.

### Preserved Evidence

Swarm failure cleanup removes a failed task's worktree and branch so future reruns are not
blocked. Before any of that happens, the task's full diff — committed *and* uncommitted, plus
the contents of untracked files — and its verification log are written to:

```
.alysis/runs/<run_id>/execution/evidence/<task_id>/
  patch.diff
  verification.log
  evidence.json
```

That directory lives in the run, not in the worktree, so cleanup proceeds exactly as configured
(`--keep-worktrees` still controls whether the worktree survives) and no work is destroyed
either way. Capture is best effort and never blocks cleanup: anything it could not read is
recorded in `evidence.json` under `errors`.

### Strict Scope Triage

Strict mode does not reject every undeclared change. After execution each out-of-scope
change is classified into one of three buckets:

| Bucket | What it covers | What happens |
| --- | --- | --- |
| `adjacent` | A file the task created in a directory `write_scope` already reaches, a sibling test file (`test_*.py`, `*_test.*`, `*.test.*`, `*.spec.*`), or a generated artifact of a declared file. | Accepted. The path is added to the task's `write_scope`, recorded in the report's **Scope Amendments** section and in a `scope_amended` event, and the task continues. |
| `protected` | Agent-internal state (`.alysis/`, `.forge/`), version-control metadata (`.git/`), and anything outside the workspace. | Always blocks. No amendment can cover a protected path, and none is suggested. |
| `unrelated` | Everything else — an edit to a module the plan never mentioned. | Blocks, as before. The error carries the full classified list and a ready-to-paste set of `write_scope` patterns so a human or the replanner can fix the plan in one step. |

Editing an existing neighbouring file is *not* adjacent: adjacency for the directory rule
means the task created the file. A task that changed only adjacent files still passes —
that is reported as an amendment, not as the "no material file changes" rejection, which
now fires only when a task produced no file changes at all.

`--scope warn` and `--scope off` are unchanged: they report and amend nothing.

## Swarm Runs

`forge swarm` is the **parallel optimization** of `forge run`, not the normal way to execute a
plan. It runs several independent tasks at once, each in its own git worktree, and integrates
their branches in batches.

Reach for it when the plan has genuinely independent tasks with precise, non-overlapping write
scopes and the wall-clock saving is worth the extra machinery it brings:

- per-task worktrees (`git_worktrees.py`),
- a batch integration gate (`integration_gate.py`),
- an optional review gate per branch (`review_gate.py`),
- an LLM merge-conflict reviewer and a resolver agent in a dedicated conflict worktree.

Every one of those is a moving part that `forge run` does not have. If the tasks are a dependency
chain — which most plans are — the swarm's batches are of size one anyway and you get the
machinery without the parallelism.

Useful controls:

- `--parallel <n>` sets worker concurrency.
- `--max-tasks <n>` limits one run.
- `--only T01,T02` runs selected tasks while still enforcing dependencies.
- `--retry-failed` and `--retry-changes-requested` include tasks that were previously not accepted.
- `--retry-merge-conflicts` re-merges task branches left in `merge_conflict`.
- `--integration-verify` controls the verification gate after a batch is integrated.
- `--replan suggest|apply` enables between-batch replanning.

Start with `--dry-run` when reviewing a plan for the first time.

### `--parallel 1` Delegates To `forge run`

A swarm of one has no parallelism to exploit, so `forge swarm --parallel 1` hands the run to the
sequential engine and says so. It only delegates when nothing swarm-specific was requested;
`--dry-run`, `--keep-worktrees`, `--retry-merge-conflicts`, an explicit `--integration-verify`,
or an active `--replan` mode all keep the swarm engine, and the reason is printed. Set
`ALYSIS_SWARM_SEQUENTIAL_DELEGATE=0` to disable delegation entirely.

## Completion Report

When `/execute plan` finishes in chat, Alysis Code prints a completion report built from the run's
artifacts — no extra model call, so it works with every provider:

- what each task did (including the worker's own one-line summary when captured),
- the exact directory and branch the merged files landed in,
- the changed-file list, with an explicit warning when the run changed no files at all,
- honest verification status, including checks that failed but were tolerated by `--verify warn`,
- grounded "try it" commands (for example `python -m http.server` for a generated `index.html`, or
  the `npm run` script found in a written `package.json`) — only suggested when the target files
  actually exist on disk.

The same report is saved to `.alysis/runs/<run_id>/execution/completion_report.md`. In the
full-screen TUI it renders as a regular assistant message above the frozen task table; a soft
interrupt (Esc/Ctrl+C) cannot stop an in-flight swarm, and a run that completes after an interrupt
still shows its report instead of silently discarding it.

## Current Scope

Forge is intentionally stricter than normal chat. It expects a concrete workspace, small scoped
tasks, and clear verification commands for strict gates.

- `forge run` (and `forge exec --pr`, its single-task form) is the strongest acceptance path
  because it wraps execution in branch, commit, verification, review, and merge gates. Plain
  `forge exec` and `forge run --no-pr` keep a simpler local execution flow and should be reviewed
  before you commit or merge results manually.
- Sequential execution and swarm workers currently run without subagents. Use top-level
  `alysis chat` or `alysis run` for delegated exploration before starting Forge execution, or
  split the plan into smaller scoped tasks.
- Strict verification needs explicit or inferable commands. If Alysis Code cannot determine what to
  run it completes the task as `completed_unverified` and merges nothing; provide `--verify-cmd`
  when you want the work actually checked.
- Verification only runs inside the `--pr` flow. Plain `forge exec` and `forge run --no-pr` do
  not gate on it, so `--verify strict` has nothing to enforce there. `forge run` uses `--pr` by
  default for exactly this reason.
- Forge does not use a persistent partial-success task status. Incomplete work is represented by
  task status, reports, verification output, review results, and execution artifacts.
- Image handling uses conservative budget reserves; Alysis Code does not claim provider-exact vision
  token accounting for Forge execution.

## Modes And Safety

Forge follows the same execution modes as the rest of Alysis Code:

- `readonly` inspects and plans only.
- `review` asks before writes and shell commands.
- `auto` can apply approved changes with fewer prompts.
- `fullaccess` disables mode-level write and shell prompts; use only in trusted workspaces.

For public projects, start with `review` and move to `auto` only after the plan, write scopes, and
verification commands are clear.

## Practical Guidance

- Keep the initial request specific enough to identify target behavior and files.
- Review task scopes before execution.
- Prefer smaller batches for unrelated subsystems.
- Treat failed verification as review evidence, not as noise to hide.
- Commit or merge only after reviewing the final diff and verification output.

See [Execution modes](../README.md#execution-modes), [Shell sandbox](shell_sandbox.md), [Security model](security_model.md),
and [MCP](mcp.md) for the lower-level controls Forge builds on.
