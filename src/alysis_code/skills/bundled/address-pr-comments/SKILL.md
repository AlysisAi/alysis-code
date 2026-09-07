---
name: address-pr-comments
description: Resolve review threads on an existing pull request and prepare a response summary.
---

Use when: The user asks to address unresolved review comments on a pull request.

Do not use when: The user wants a fresh code review or no review threads exist yet.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Read repository instructions and inspect the current branch and working tree.
2. Enumerate unresolved threads with their URL, file, line, author, and request. If `gh` is installed and authenticated, `gh api` is a suggested source. Otherwise use supplied comments and git context, then tell the user which forge or PR web UI steps remain.
3. Decide whether to accept or disagree with each request. Never silently skip a thread.
4. For each accepted request, make the smallest scoped change and create a focused commit only when commits are authorized. If commits are out of scope, identify the ready change instead.
5. For each rejected request, state the technical reason and cite concrete code or test evidence.
6. Remove temporary reproduction artifacts and instrumentation only when they were created during the current task. Preserve pre-existing and user-provided files.
7. After cleanup, run the final focused verification and rerun it after any later edit or cleanup. Recheck every thread.
8. End with a reply-ready summary mapping each thread to its commit or explicit disagreement, plus any replies the user must post in the forge UI.
