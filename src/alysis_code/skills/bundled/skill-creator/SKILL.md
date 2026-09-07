---
name: skill-creator
description: Create or update a reusable Alysis Code skill through its supported scaffold and validation workflow.
---

Use when: The user wants to create or revise a reusable Alysis Code skill.

Do not use when: The user needs a one-off answer, repository change, or prompt that should not become a skill.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Interview only for missing decisions that materially affect the skill: target requests, trigger boundary, required workflow, authorization limits, and reusable resources.
2. Inspect an existing skill before updating it. Preserve supported metadata and resources that still serve the requested workflow.
3. For a new skill, use `alysis skill init <name>` as the suggested scaffold so naming, paths, and frontmatter follow the same conventions as the CLI. Do not recreate scaffold logic in prose or ad hoc files.
4. Make the description concise and discriminating. Put essential model instructions in `SKILL.md`; add scripts, references, or assets only when they provide a concrete reusable benefit.
5. Keep instructions imperative, scoped, and explicit about external actions. Do not assume tools or services unavailable in the target environment.
6. Exercise any new script and test at least one realistic invocation when practical.
7. Remove temporary test inputs, generated scratch files, and other throwaways only when created during the current task; preserve pre-existing and user-provided files. Generated workflows must use this ownership boundary, clean those throwaways before final verification, and rerun verification after any later cleanup or edit.
8. Finish with the suggested command `alysis skill validate <skill-path>`. Resolve validation errors and report warnings or remaining behavioral uncertainty.
