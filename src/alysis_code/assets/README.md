# Assets

This package implements assets used by planning and execution workflows. Assets
can represent local files, text, images, OCR output, and derived comprehension
records associated with a run.

## Contents

- `models.py`, `paths.py`, `index.py`, and `ingestion.py` define and persist
  asset records.
- `ocr.py`, `comprehender.py`, and `prompts.py` derive text and summaries.
- `planner_context.py`, `replanner_context.py`, `worker_section.py`, and
  `worker_tools.py` prepare assets for planning and execution.
- `legacy_migration.py` handles older attached-asset layouts.
- `untrusted_content.py` wraps extracted asset text.

## Scope

Asset content can come from repository files, user uploads, OCR, or generated
summaries. Keep it bounded and provenance-aware before exposing it to prompts.

Asset records are run artifacts. Mutations should preserve index consistency
and avoid silently deleting original stored files.

## Development

Schema, migration, and prompt-context changes should include regression coverage
for existing asset records.

## See Also

- [Forge](../../../docs/forge.md)
- [Security model](../../../docs/security_model.md)
