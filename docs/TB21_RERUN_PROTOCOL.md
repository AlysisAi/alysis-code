# Terminal-Bench 2.1 re-run protocol (0.13.0 reliability wave)

This is the operator runbook for re-scoring the 0.13.0 build against the
Terminal-Bench 2.1 baseline. Its whole purpose is a **clean, attributable
comparison**: one pinned build, one variable changed (the fixes), nothing
touched between trials. Follow it top to bottom.

## The baseline you are beating

The pre-fix production build (Terminal-Bench 2.1, 89 tasks, 3 trials):

| metric | baseline |
| --- | --- |
| mean score | **59.6%** (55 / 56 / 48 of 89) |
| solved 3/3 (solid) | **37** |
| flaky (1/3 or 2/3) | **30** |
| never solved (0/3) | **22** |
| compute burned on reward-0 tasks | **~30%** |
| `NonZeroAgentExitCodeError` | on `compile-compcert` in **all 3** trials |

## What changed (why a re-run is warranted)

The six fixes (PR1-PR6) and their most direct Terminal-Bench targets:

- **PR2/PR3** — `compile-compcert`, `polyglot-rust-c`: a budget stop now exits 0
  with `stop_reason: "run_budget_exhausted"` instead of raising
  `NonZeroAgentExitCodeError`; shell waits are budget-preemptible; dispatch
  overhead is measured.
- **PR4** — `hf-model-inference`, `configure-git-webserver`, `qemu-alpine-ssh`:
  persist-mode services survive agent exit; non-interactive shell defaults stop
  silent prompt stalls.
- **PR5** — `filter-js-from-html`, `gcode-to-text`: wholesale-rewrite warning and
  bounded thrash notice.
- **PR1/PR6** — cross-cutting: no secret can reach a log; every run records a
  non-fakeable build identity and effective config so a score is attributable.

The complete model-facing footprint of the wave is in `CHANGELOG.md` under
"Model-visible string changes in 0.13.0" — read it before you start so you know
exactly what the model will and will not see differently.

---

## Step 0 — Pin the build

1. Check out the release tag / commit for the wave and confirm the tree is
   clean. **Do not** run against "latest main"; an unpinned build is the exact
   mistake that made the earlier campaign's numbers unattributable.
2. Stamp the build identity so every run can prove which commit it is:

   ```bash
   python3 scripts/generate_build_info.py
   python3 scripts/generate_build_info.py --print-only   # sanity: prints commit, dirty: no
   ```

   The stamp is written to `src/alysis_code/_build_info.py`. After you
   build the wheel/image, restore the committed dev default so the repo never
   carries a stamp from a past build:

   ```bash
   git checkout -- src/alysis_code/_build_info.py
   ```

3. Build the agent image/wheel the campaign will use, from that stamped tree.
   `alysis --version` on the built artifact must show `dirty: no` and a real
   commit (not `commit: unknown`). `benchmarks/terminal_bench/setup.sh` already
   greps for exactly this.

## Step 1 — Pre-flight on the runner box

The sandbox that produced these fixes has **no pytest and cannot import the
package** (Python 3.10 vs the package's 3.11). The runner box can and must do
the full check.

1. **Full test suite** (runner box has the deps):

   ```bash
   python3 -m pytest -q
   ```

   This must pass before a single trial runs. The sandbox could not run it;
   you are the gate.

2. **Dependency-free stdlib sanity** — one command, no pytest, no third-party
   packages, exercises every dependency-light module the wave added:

   ```bash
   bash scripts/run_all_stdlib_tests.sh
   ```

   Use this as a fast smoke check even where pytest is unavailable.

3. **Pin the run environment.** Export exactly these, and change nothing else
   for the whole campaign:

   ```bash
   export ALYSIS_RUN_BUDGET_SECONDS=10800     # ~3.5h, matches Harbor's per-task allowance
   export ALYSIS_REQUIRE_CLEAN_BUILD=1         # refuse to start against an unidentifiable build
   ```

   - `ALYSIS_RUN_BUDGET_SECONDS=10800` sets the wave's wall-clock budget to
     Harbor's window so the budget watchdog degrades a stuck run *inside* the
     harness timeout rather than being killed by it — that is what converts the
     old `NonZeroAgentExitCodeError` into a clean exit-0 budget stop.
   - `ALYSIS_REQUIRE_CLEAN_BUILD=1` makes the run refuse to start if the build
     cannot identify itself, so no trial can silently run against the wrong tree.
   - Leave the sampling controls (`ALYSIS_SAMPLING_*`) **unset** unless you are
     deliberately pinning them; unset means the request is byte-identical to the
     pre-fix transport, which is the honest apples-to-apples comparison. If you
     do pin them, pin them identically across all three trials.

4. **Set a canary key.** Run the campaign with a *known, disposable* value in the
   API-key variable the harness reads, and record that value — the regression
   suite scans every artifact for it to prove PR1 redaction held:

   ```bash
   export ALYSIS_REGRESSION_CANARY_VALUE="$ALYSIS_API_KEY"   # whatever the campaign used
   ```

## Step 2 — Regression suite (defects must stay fixed)

Before the full campaign, confirm the six defects are actually fixed on this
build. Run the seven regression tasks and assert on their artifacts:

```bash
python3 scripts/regression_suite.py --run \
    --harbor-cmd 'bash benchmarks/terminal_bench/run_harbor_tbench.sh' \
    --artifacts-root ./runs/regression \
    --canary-value "$ALYSIS_REGRESSION_CANARY_VALUE"
```

It invokes Harbor once per task (`compile-compcert`, `hf-model-inference`,
`configure-git-webserver`, `polyglot-rust-c`, `qemu-alpine-ssh`,
`filter-js-from-html`, `gcode-to-text`) with `--include-task-name`, then asserts
the defect-specific expectation for each. **Exit code 0 is required to proceed.**
A `FAIL` here means a fix regressed — stop and investigate; do not run the
campaign. `WARN`/`SKIP` are acceptable (e.g. a task that finished within budget
has no budget stop to assert).

You can re-check an already-collected archive offline, without Harbor:

```bash
python3 scripts/regression_suite.py --check-artifacts ./runs/regression/compile-compcert \
    --task compile-compcert --canary-value "$ALYSIS_REGRESSION_CANARY_VALUE"
```

## Step 3 — Canary suite (nothing that worked broke)

Confirm the always-solved tasks still pass. **First** open your baseline archive,
read the 37 tasks that scored 3/3, and reconcile `scripts/canary_tasks.txt`
against them (it ships a stratified candidate set with an explicit TODO — the
slugs are placeholders until you confirm them). Optionally add ~10 always-solved
SWE-bench instances per the note at the bottom of that file.

```bash
python3 scripts/canary_suite.py --run \
    --tasks scripts/canary_tasks.txt \
    --harbor-cmd 'bash benchmarks/terminal_bench/run_harbor_tbench.sh' \
    --artifacts-root ./runs/canary
```

The default baseline expects **every** canary task to pass; any failure exits
non-zero and is a regression to investigate before the campaign. The output
table also reports dispatch overhead and cost per task — eyeball it for a silent
efficiency regression even when every task still passes.

## Step 4 — The 3-trial campaign

Only after Steps 1-3 are green:

1. Run **three** full trials of the 89-task Terminal-Bench 2.1 set, on the one
   pinned tag, with the Step-1 environment.
2. **Change one variable only** — the build. Same dataset, same harness, same
   timeouts, same env, same everything else as the baseline campaign.
3. **Do nothing between trials.** No rebuild, no config edit, no cache clear, no
   dependency bump. The three trials exist to measure the build's *own* variance;
   any change between them destroys that measurement.
4. Archive each trial's artifacts separately (the runtime `.alysis/` and the
   crash-diagnostics JSONL) so per-trial cross-checks and the canary/regression
   assertions can be re-run offline.

## Step 5 — Success criteria (measured against the baseline)

**Primary (the fixes are about reproducibility, so these are the ones that
matter):**

- **Solid set 37 → 45+**: tasks solved 3/3 rises from 37 to at least 45.
- **Flaky 30 → &lt;20**: tasks that land 1/3 or 2/3 falls from 30 to under 20.

**Secondary:**

- **Zero `NonZeroAgentExitCodeError`** across all three trials (was: all three).
- **Wasted compute on reward-0 tasks &lt; 15%** (was ~30%).
- **Mean score** at or above the 59.6% baseline.

## Step 6 — Read the result honestly

> **If the mean score went up but the flaky count did not come down, the fixes
> underperformed — investigate before celebrating.**

A higher mean with unchanged flakiness usually means variance moved a few
borderline tasks over the line by luck, not that the run got more reproducible —
which is exactly what this wave was built to deliver. The primary criteria are
the solid/flaky counts, not the headline percentage. Cross-check the per-trial
agreement (which tasks flipped between trials) before drawing any conclusion, and
attribute every number to the stamped commit the runs recorded in their
`config_snapshot` build identity.
```
