from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.review_gate import ReviewOutcome


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _prepare_run_with_task(runner: CliRunner, repo: Path, tmp_path: Path) -> tuple[str, Path]:
    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input="/goal Review test\n/task Implement src/feature.py\n/done\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    plan_path = run_dir / "plan" / "plan.json"
    plan = _load_json(plan_path)
    return str(plan["tasks"][0]["id"]), run_dir


def test_review_command_exit_zero_when_approved(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, run_dir = _prepare_run_with_task(runner, repo, tmp_path)

    def fake_review_task(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        json_path = run_dir / "execution" / "reviews" / f"{task_id}.json"
        md_path = run_dir / "execution" / "reviews" / f"{task_id}.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}", encoding="utf-8")
        md_path.write_text("# review\n", encoding="utf-8")
        return ReviewOutcome(
            task_id=task_id,
            approved=True,
            confidence="high",
            summary="ok",
            blocking_issues_count=0,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=md_path,
        )

    monkeypatch.setattr(cli_mod, "review_task", fake_review_task)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "review",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "Approved: yes" in result.output


def test_review_command_exit_zero_when_not_approved(tmp_path: Path, monkeypatch) -> None:
    """A completed review that rejects work is not a failed command."""
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, run_dir = _prepare_run_with_task(runner, repo, tmp_path)

    def fake_review_task(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        json_path = run_dir / "execution" / "reviews" / f"{task_id}.json"
        md_path = run_dir / "execution" / "reviews" / f"{task_id}.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}", encoding="utf-8")
        md_path.write_text("# review\n", encoding="utf-8")
        return ReviewOutcome(
            task_id=task_id,
            approved=False,
            confidence="medium",
            summary="changes needed",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=md_path,
        )

    monkeypatch.setattr(cli_mod, "review_task", fake_review_task)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "review",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "Approved: no" in result.output
