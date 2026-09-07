---
name: fix-ci
description: Use for a failing automated build or repository check with run output/logs; reproduce the failed step, repair it, and verify the check.
---

Use when: The user asks to repair an observed automated build or repository check failure using its run output or logs.

Do not use when: The failure is purely local, no failing automated run is identified, or the user only asks for an explanation.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Read repository instructions, inspect the branch state, and identify the exact failing job, step, and first useful error.
2. If `gh` is installed and authenticated, use `gh run view` and its failed logs as suggested evidence. Otherwise use supplied logs and git history, then tell the user how to retrieve or rerun the job in the forge UI.
3. Inspect the workflow definition and reproduce the failing command locally when it is cheap and safe. Match relevant versions and environment settings without exposing secrets.
4. Add or tighten a focused regression test when the defect is in repository code. Make the smallest fix that addresses the evidenced cause; do not weaken or skip the CI gate.
5. Remove temporary reproduction artifacts and instrumentation only when they were created during the current task. Preserve pre-existing and user-provided files.
6. After cleanup, perform the final authoritative rerun of the originally failing command, then the nearest dependent checks. Rerun it again after any later change. Treat a remote rerun as an external action that needs normal authorization.
7. Report the failing cause, changed files, local verification, and any remote check still required.

Never force-push, bypass hooks, or rewrite workflow history.
