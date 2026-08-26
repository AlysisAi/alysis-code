from __future__ import annotations

import logging
from pathlib import Path

from alysis_code.assets.replanner_context import summarize_prior_asset_usage
from alysis_code.assets.usage_logger import AssetUsageLogger
from alysis_code.forge import create_plan_run


def test_prior_usage_summary_reads_jsonl(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    logger = AssetUsageLogger(run_paths=paths, task_id="T03")
    logger.allocation_decision(asset_id="ast_one", mode="full_inline")
    logger.allocation_decision(asset_id="ast_two", mode="reference_only")
    logger.inline_injection(asset_id="ast_one", kind="image")
    logger.asset_read(asset_id="ast_one", focus=False, chars=120, cached=False)
    logger.asset_read(asset_id="ast_two", focus=True, chars=80, cached=True)
    logger.asset_load(asset_id="ast_two", kind="text", chars=90)
    logger.summary(primary_count=2, may_need_count=1, pinned_count=1)

    summary = summarize_prior_asset_usage(run_paths=paths, task_id="T03")

    assert summary is not None
    assert summary.attempts_seen == 1
    assert summary.primary_count == 2
    assert summary.may_need_count == 1
    assert summary.pinned_count == 1
    assert summary.reads_per_asset == {"ast_one": 1, "ast_two": 1}
    assert summary.loads_per_asset == {"ast_two": 1}
    assert summary.inline_injections_per_asset == {"ast_one": 1}
    assert "## Prior Attempt Asset Interaction" in summary.text_block
    assert "ast_one" in summary.text_block


def test_prior_usage_summary_empty_log_returns_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    paths.execution_asset_usage_dir.mkdir(parents=True, exist_ok=True)
    (paths.execution_asset_usage_dir / "T01.jsonl").write_text("", encoding="utf-8")

    assert summarize_prior_asset_usage(run_paths=paths, task_id="T01") is None


def test_prior_usage_summary_skips_malformed_lines(
    tmp_path: Path,
    caplog,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    path = paths.execution_asset_usage_dir / "T01.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{bad json}\n{"event":"asset_read","asset_id":"ast_ok","focus":false,"chars":4}\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        summary = summarize_prior_asset_usage(run_paths=paths, task_id="T01")

    assert summary is not None
    assert summary.reads_per_asset == {"ast_ok": 1}
    assert "skipped malformed line" in caplog.text
