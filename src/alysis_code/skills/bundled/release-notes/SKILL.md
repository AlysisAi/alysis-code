---
name: release-notes
description: Draft project release notes or a changelog from merged history; not for conceptual explanations about release notes.
---

Use when: The user requests release notes or a changelog for a git revision range.

Do not use when: The user asks only for a conceptual explanation, or asks to publish, tag, or deploy a release without requesting notes.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Read repository instructions and existing release-note or changelog style.
2. Resolve the requested range. If none is supplied, use the latest reachable tag through `HEAD`; `git describe --tags --abbrev=0` and first-parent `git log` are suggested evidence. If no tag exists, state the fallback range.
3. Inspect merged commits and relevant diffs. Treat Conventional Commit types as classification hints, not as proof of user impact.
4. Group user-visible changes by type, such as features, fixes, performance, and documentation. Omit internal churn unless it affects operators or contributors.
5. Call out breaking changes explicitly, including `!` subjects and `BREAKING CHANGE` trailers, and explain the required migration when evidence exists.
6. Credit contributors or link issues only when the repository history provides reliable identifiers.
7. Write to the requested file or return a draft. Do not change versions, create tags, publish, or push unless separately asked.
8. State the revision range and any ambiguous or excluded items.
