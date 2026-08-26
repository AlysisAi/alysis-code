# Alysis Code QA UX Smoke Battery

This harness runs user-facing Alysis Code UX smoke scenarios as real CLI subprocesses, captures transcripts, applies explicit quality rules, and writes a Markdown report under `qa_reports/`.

## Run locally

```bash
python -m scripts.qa list
python -m scripts.qa run
python -m scripts.qa run --scenarios C,D,G
python -m scripts.qa run --output qa_reports/manual
```

By default the harness runs the local source CLI with `python -m alysis_code.cli`. To force an installed binary:

```bash
python -m scripts.qa run --cli alysis
```

The harness starts a local stdlib OpenAI-compatible mock server. It never calls real LLM providers.

## Reports

Each run writes:

- `qa_reports/<timestamp>/report.md`
- `qa_reports/<timestamp>/transcripts/*.log` for `NO_COLOR=1` rule checks
- `qa_reports/<timestamp>/transcripts_color/*.log` for human visual review
- `qa_reports/<timestamp>/sessions/*.jsonl` when Alysis Code produced session logs
- `qa_reports/<timestamp>/workdirs/` preserved per scenario for inspection

The committed baseline lives at `qa_reports/baseline/report.md`.

## Add a scenario

Add one `Scenario` entry in the relevant file under `scripts/qa/scenarios/`. A scenario provides:

- `name`
- `family`
- `description`
- `setup(work_dir)` to create a synthetic repo
- `drive(harness, work_dir)` returning a `RunSpec`

If a scenario cannot be automated yet, add it with `skip_reason`. Skips are explicit in the report.

## Add a quality rule

Add one `QualityRule` entry in `scripts/qa/quality_rules.py`. Rules return excerpts for violations and include severity: `blocker`, `high`, `medium`, or `low`.

## Pytest

Harness sanity tests run with default pytest:

```bash
pytest tests/qa/test_harness.py
```

The full battery is marked `qa` and is skipped by default unless explicitly selected:

```bash
pytest -m qa tests/qa/test_qa_battery.py
```

## Raw Agent proxy harness

The raw-agent proxy harness exercises the normal `alysis run --benchmark`
path as real CLI subprocesses against a local OpenAI-compatible mock provider.
It is meant for benchmark-readiness regressions around material work,
verification repair, completion-gate nudges, focused test discovery, and
required diff review without contacting live providers.

```bash
PYTHONPATH=src:. python -m scripts.qa.raw_agent_proxy --list
PYTHONPATH=src:. python -m scripts.qa.raw_agent_proxy \
  --output qa_reports/raw_agent_proxy
PYTHONPATH=src:. python -m scripts.qa.raw_agent_proxy \
  --scenarios pytest_contract_smoke,completion_gate_forced_diff \
  --scenario-timeout-s 120 \
  --output qa_reports/raw_agent_proxy_focus
```

By default the harness starts the local mock provider, writes an isolated config
and data directory for each scenario, and runs the source CLI with
`python -m alysis_code.cli run --benchmark`. Use
`--cli-command alysis` only when intentionally testing an installed binary. Use
`--no-mock-provider` only for explicit live/provider shakedowns; that path uses
the caller's environment and is not part of the deterministic local gate.

Each run writes:

- `raw_agent_proxy_report.md`
- `raw_agent_proxy_metrics.json`
- `raw_agent_proxy_runs.json`
- `transcripts/*.log`
- `workdirs/<scenario>/` with the disposable repo, config, data, and session
  logs

## Current limitations

Raw TTY-only flows such as exact arrow-key navigation in `/config` and live approval prompts are represented as skipped scenarios until a pexpect-backed driver is added. The current battery still exercises non-TTY fallback paths and captures visual transcripts for subprocess-driven flows.
