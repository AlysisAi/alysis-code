# QA scripts

These scripts provide focused repository and runtime checks that complement the pytest suite.

## Repository checks

Check that imports from the runtime package have matching dependency declarations:

```bash
uv run python scripts/qa/check_deps.py
```

Check that the public tree excludes internal documentation, caches, local runtime output,
credential-bearing file types, and oversized artifacts. The check also validates relative Markdown
links, including exact filename and directory capitalization, so links work on Linux as well as
case-insensitive filesystems:

```bash
uv run python scripts/qa/check_public_repo.py
```

## Runtime harness

`raw_agent_proxy.py` runs deterministic one-shot scenarios against the local mock provider. It
exercises completion, verification, and diff-review behavior without contacting a hosted model.

```bash
PYTHONPATH=src:. uv run python -m scripts.qa.raw_agent_proxy --list
PYTHONPATH=src:. uv run python -m scripts.qa.raw_agent_proxy \
  --output qa_reports/raw_agent_proxy
```

Reports, transcripts, temporary repositories, and session files are written beneath the selected
output directory. `qa_reports/` is ignored by Git.

`mock_llm.py` supports this harness and is not intended as a standalone command.

Session logs may contain project information. Do not commit them or attach them to public issues
without reviewing and sanitizing their contents.
