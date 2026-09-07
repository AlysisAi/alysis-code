---
name: commit
description: Create a Conventional Commit from the user's changes, staging a coherent set only when nothing is staged; never amend or push unasked.
---

Use when: The user asks to commit their changes, whether or not anything is staged yet.

Do not use when: There are no changes to commit, or the user asks to amend, squash, rebase, or push.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Read repository instructions and inspect `git status --short`, `git diff --cached --stat`, and `git diff --stat`.
2. If the index already holds changes, commit only the staged content. Warn when unstaged edits make the staged result incomplete, but do not add them unless the user asks.
3. If nothing is staged, propose the smallest coherent set from the working tree that matches the request: name each path and why it belongs. Stage those paths explicitly with `git add <path>`; never use `git add -A`, `git add .`, or interactive staging. If the changes clearly belong to several commits, say so and commit only the first set, or ask.
4. Exclude secrets, credentials, generated or build output, debug artifacts, and unrelated edits from any staging you perform. Stop and report when the staged diff still contains a material concern.
5. Read the complete staged diff before writing the message. Base the message only on staged content.
6. Choose the narrowest accurate Conventional Commit type and optional scope. Write an imperative subject that describes the outcome; add a body only when it explains motivation, risk, or a breaking change.
7. Use `git commit` as a suggested command only after the staged scope and message are sound. If hooks fail, report the failure and keep the index intact; never bypass hooks unless explicitly asked.
8. Report the commit hash, subject, the paths committed, and any paths deliberately left out. Never amend or push unless the user asks.
