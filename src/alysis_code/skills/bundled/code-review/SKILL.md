---
name: code-review
description: Review a diff or branch broadly across correctness, tests, security, and conventions; use when no narrower review workflow is requested.
---

Use when: The user requests a broad structured review of a diff, commit, or branch and no narrower review workflow governs the request.

Do not use when: The user asks to implement changes without requesting review.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Read applicable `AGENTS.md`, `CLAUDE.md`, and `CONVENTIONS.md` files before judging the change.
2. Establish the review range from the user's target. Otherwise inspect the working tree or compare the branch with its merge base. Use `git status`, `git diff`, and `git log` as suggested evidence sources.
3. Trace changed behavior through callers, data boundaries, error paths, and tests. Check correctness, regressions, missing tests, security impact, and repository conventions.
4. Run only focused checks that materially test a suspected problem. Distinguish verified defects from questions or uncertainty.
5. Do not edit the code unless the user separately asks for fixes.
6. Report findings first, ordered by severity. Give each finding a concise title, impact, evidence, and exact `file:line`. Avoid style-only findings unless a repository rule requires them.
7. If no actionable findings remain, say so and list any residual testing or scope gaps.
