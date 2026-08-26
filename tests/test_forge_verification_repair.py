"""Verification outcomes that keep the work: unverified completion, repair, evidence.

Three behaviours are pinned here:

(a) writes succeeded and no authoritative verification command exists -> the task
    completes as ``completed_unverified`` with its files intact, never a failure;
(b) verification ran and failed -> the failing output is fed back to the executing
    agent, and a repair that turns the gate green still merges;
(c) the repair budget is exhausted -> the task fails honestly, but its patch
    artifact is on disk.

Plus the swarm-side guarantee that a failed task's diff and verification log are
written into the run before its worktree is removed.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig
from alysis_code.failed_task_evidence import (
    METADATA_FILE_NAME,
    PATCH_FILE_NAME,
    VERIFICATION_LOG_FILE_NAME,
    evidence_dir_for,
    preserve_failed_task_evidence,
)
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.swarm_orchestrator import run_swarm
from alysis_code.swarm_worker import TaskWorkerResult
from alysis_code.verification_repair import (
    DEFAULT_VERIFICATION_REPAIR_ATTEMPTS,
    TASK_STATUS_COMPLETED_UNVERIFIED,
    RepairAttemptExecution,
    build_repair_instruction,
    resolve_repair_attempt_budget,
    run_verification_repair_loop,
    verification_failure_excerpts,
)
from alysis_code.verify_gate import (
    ResolvedVerifyCommands,
    VerifyCommandResult,
    VerifyRunResult,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "--no-verify", "-m", "init")


def _prepare_plan(repo: Path) -> tuple[Path, str]:
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Execute tasks safely"
    plan["summary"] = "Execute tasks safely"
    task = add_task(
        plan,
        title="Implement feature slice",
        description="Implement feature slice",
        estimated_files=["src/file.py"],
    )
    task["write_scope"] = ["src/**"]
    save_plan(paths, plan)
    return paths.plan_json_path, str(task["id"])


def _run_dir(repo: Path) -> Path:
    pointer = json.loads((repo / ".alysis" / "current_run.json").read_text(encoding="utf-8"))
    return repo / pointer["run_path"]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_result(*, passed: bool, artifact_path: Path, output: str = "") -> VerifyRunResult:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(output or ("ok\n" if passed else "boom\n"), encoding="utf-8")
    return VerifyRunResult(
        commands=["pytest -q"],
        command_results=[
            VerifyCommandResult(
                "pytest -q",
                0 if passed else 1,
                output or ("ok\n" if passed else "E   assert 1 == 2\n"),
                # A zero exit is only a pass when the gate saw tests actually run.
                real_execution=True,
            )
        ],
        artifact_path=artifact_path,
    )


def _exec_argv(repo: Path, task_id: str, *extra: str) -> list[str]:
    return [
        "forge",
        "exec",
        task_id,
        "--path",
        os.fspath(repo),
        "--model",
        "test-model",
        "--api-key",
        "k",
        "--no-log",
        "--pr",
        "--verify",
        "strict",
        *extra,
    ]


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(repo),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


# --------------------------------------------------------------------------- #
# (a) no authoritative command + successful writes -> completed_unverified
# --------------------------------------------------------------------------- #


def test_exec_strict_without_authoritative_commands_completes_unverified(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_path, task_id = _prepare_plan(repo)

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        target = Path(root) / "src" / "file.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('shipped')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")
    # verify_gate suppresses generic fallbacks for workspaces it cannot confidently
    # verify; that empty selection is exactly the input under test.
    monkeypatch.setattr(
        cli_mod,
        "resolve_authoritative_task_verify_command_selection",
        lambda **_kwargs: ResolvedVerifyCommands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification must not run when no command exists")
        ),
    )

    result = CliRunner().invoke(
        alysis_app,
        _exec_argv(repo, task_id, "--machine"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    final_plan = _load_json(plan_path)
    task = final_plan["tasks"][0]
    assert task["status"] == TASK_STATUS_COMPLETED_UNVERIFIED

    # The work is intact: committed on the task branch, not thrown away.
    branch = str(task["branch"])
    assert _branch_exists(repo, branch)
    assert "src/file.py" in _git(repo, "show", "--name-only", "--format=", branch).stdout
    # ...and the run left the repository back on the base branch, unmerged.
    assert _current_branch(repo) == "main"
    assert "src/file.py" not in _git(repo, "show", "--name-only", "--format=", "main").stdout

    events = [
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]
    unavailable = [e for e in events if e["event"] == "verification_unavailable"]
    assert unavailable, events
    assert unavailable[-1]["data"]["blocking"] is False
    assert unavailable[-1]["data"]["outcome"] == TASK_STATUS_COMPLETED_UNVERIFIED
    assert [e for e in events if e["event"] == "task_completed"]
    assert not [e for e in events if e["event"] == "task_failed"]
    terminal = [e for e in events if e["event"] == "run_completed"][-1]
    assert terminal["data"]["ok"] is True


def test_exec_strict_without_commands_still_fails_when_the_work_itself_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """Missing tooling is not a failure; a task that did nothing still is."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_path, task_id = _prepare_plan(repo)

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")
    monkeypatch.setattr(
        cli_mod,
        "resolve_authoritative_task_verify_command_selection",
        lambda **_kwargs: ResolvedVerifyCommands(
            commands=(),
            source="task_refinement.no_authoritative_commands",
        ),
    )

    result = CliRunner().invoke(
        alysis_app,
        _exec_argv(repo, task_id),
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert _load_json(plan_path)["tasks"][0]["status"] != TASK_STATUS_COMPLETED_UNVERIFIED


# --------------------------------------------------------------------------- #
# (b) failing verification -> repair attempts -> pass -> merged
# --------------------------------------------------------------------------- #


def test_exec_strict_repairs_failing_verification_then_merges(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_path, task_id = _prepare_plan(repo)
    verify_artifact = _run_dir(repo) / "execution" / "verify" / f"{task_id}.txt"

    agent_instructions: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        agent_instructions.append(instruction)
        target = Path(root) / "src" / "file.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        if len(agent_instructions) == 1:
            target.write_text("value = 1\n", encoding="utf-8")
        else:
            target.write_text("value = 2\n", encoding="utf-8")
        return 0

    verify_calls: list[int] = []

    def fake_verify(**_kwargs) -> VerifyRunResult:
        verify_calls.append(len(verify_calls) + 1)
        if len(verify_calls) == 1:
            return _verify_result(
                passed=False,
                artifact_path=verify_artifact,
                output="E   assert value == 2\nsrc/file.py:1: AssertionError\n",
            )
        return _verify_result(passed=True, artifact_path=verify_artifact)

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli_mod, "run_task_verification", fake_verify)
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")

    result = CliRunner().invoke(
        alysis_app,
        _exec_argv(repo, task_id, "--verify-cmd", "pytest -q"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert _load_json(plan_path)["tasks"][0]["status"] == "done"

    # One repair attempt ran, and it was prompted with the failing output.
    assert len(agent_instructions) == 2
    assert len(verify_calls) == 2
    repair_prompt = agent_instructions[1]
    assert "Verification Repair Attempt" in repair_prompt
    assert "assert value == 2" in repair_prompt
    assert "pytest -q" in repair_prompt
    # The task's own instruction is repeated so the repair session has its context.
    assert "Implement feature slice" in repair_prompt

    # The repaired work merged into the base branch.
    assert _current_branch(repo) == "main"
    assert (repo / "src" / "file.py").read_text(encoding="utf-8") == "value = 2\n"
    assert "src/file.py" in _git(repo, "log", "--name-only", "--format=", "main").stdout


def test_exec_strict_repair_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_path, task_id = _prepare_plan(repo)
    verify_artifact = _run_dir(repo) / "execution" / "verify" / f"{task_id}.txt"
    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        agent_calls.append(instruction)
        target = Path(root) / "src" / "file.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"value = {len(agent_calls)}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        cli_mod,
        "run_task_verification",
        lambda **_kwargs: _verify_result(passed=False, artifact_path=verify_artifact),
    )
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")

    result = CliRunner().invoke(
        alysis_app,
        _exec_argv(
            repo,
            task_id,
            "--verify-cmd",
            "pytest -q",
            "--verify-repair-attempts",
            "0",
        ),
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert _load_json(plan_path)["tasks"][0]["status"] == "verify_failed"
    assert len(agent_calls) == 1


# --------------------------------------------------------------------------- #
# (c) repair exhausted -> task_failed, but the patch artifact survives
# --------------------------------------------------------------------------- #


def test_exec_strict_exhausted_repair_fails_task_but_keeps_patch_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan_path, task_id = _prepare_plan(repo)
    run_dir = _run_dir(repo)
    verify_artifact = run_dir / "execution" / "verify" / f"{task_id}.txt"
    patch_path = run_dir / "execution" / "patches" / f"{task_id}.diff"

    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        agent_calls.append(instruction)
        target = Path(root) / "src" / "file.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Every attempt changes something, so the loop is bounded by the budget
        # rather than by "the agent gave up".
        target.write_text(f"value = {len(agent_calls)}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        cli_mod,
        "run_task_verification",
        lambda **_kwargs: _verify_result(passed=False, artifact_path=verify_artifact),
    )
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")

    result = CliRunner().invoke(
        alysis_app,
        _exec_argv(repo, task_id, "--verify-cmd", "pytest -q", "--machine"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert _load_json(plan_path)["tasks"][0]["status"] == "verify_failed"

    # One execution plus the full default repair budget, and no more.
    assert len(agent_calls) == 1 + DEFAULT_VERIFICATION_REPAIR_ATTEMPTS

    # The work is not destroyed: the patch artifact and the verification log stay.
    assert patch_path.exists()
    assert patch_path.read_text(encoding="utf-8").strip()
    assert verify_artifact.exists()

    events = [
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{") and line.endswith("}")
    ]
    failed = [e for e in events if e["event"] == "task_failed"]
    assert failed
    repair = failed[-1]["data"]["verification_repair"]
    assert repair["attempts_used"] == DEFAULT_VERIFICATION_REPAIR_ATTEMPTS
    assert repair["exhausted"] is True
    assert repair["passed"] is False


# --------------------------------------------------------------------------- #
# repair-loop policy (no git, no provider)
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, *, passed: bool, summary: str = "", category: str | None = None) -> None:
        self.all_passed = passed
        self.summary = summary or ("passed" if passed else "failed")
        self.failed_commands = [] if passed else ["pytest -q"]
        self.failure_category_value = category
        self.command_results = []


def test_repair_loop_stops_as_soon_as_verification_passes() -> None:
    attempts: list[int] = []

    def attempt(n: int, _failing: object) -> RepairAttemptExecution:
        attempts.append(n)
        return RepairAttemptExecution(
            agent_exit_code=0,
            verify_result=_FakeResult(passed=True),
            committed=True,
        )

    outcome = run_verification_repair_loop(
        initial_result=_FakeResult(passed=False),
        max_attempts=3,
        attempt_repair=attempt,
    )
    assert attempts == [1]
    assert outcome.passed is True
    assert outcome.repaired is True
    assert outcome.exhausted is False


def test_repair_loop_spends_the_whole_budget_then_reports_exhausted() -> None:
    attempts: list[int] = []

    def attempt(n: int, _failing: object) -> RepairAttemptExecution:
        attempts.append(n)
        return RepairAttemptExecution(
            agent_exit_code=0,
            verify_result=_FakeResult(passed=False),
            committed=True,
        )

    outcome = run_verification_repair_loop(
        initial_result=_FakeResult(passed=False),
        max_attempts=2,
        attempt_repair=attempt,
    )
    assert attempts == [1, 2]
    assert outcome.exhausted is True
    assert outcome.attempts_used == 2


def test_repair_loop_stops_when_an_attempt_cannot_reverify() -> None:
    calls: list[int] = []

    def attempt(n: int, _failing: object) -> RepairAttemptExecution:
        calls.append(n)
        return RepairAttemptExecution(
            agent_exit_code=0,
            verify_result=None,
            error="the repair attempt made no repository changes",
        )

    outcome = run_verification_repair_loop(
        initial_result=_FakeResult(passed=False),
        max_attempts=3,
        attempt_repair=attempt,
    )
    assert calls == [1]
    assert outcome.skipped_reason == "the repair attempt made no repository changes"


def test_repair_loop_does_not_run_for_unrepairable_failures() -> None:
    def attempt(_n: int, _failing: object) -> RepairAttemptExecution:
        raise AssertionError("infrastructure failures must not be repaired by editing code")

    outcome = run_verification_repair_loop(
        initial_result=_FakeResult(passed=False, category="infra_unavailable"),
        max_attempts=2,
        attempt_repair=attempt,
        repairable=lambda result: result.failure_category_value != "infra_unavailable",
    )
    assert outcome.attempts_used == 0
    assert outcome.skipped_reason


def test_repair_budget_precedence_and_clamping() -> None:
    assert resolve_repair_attempt_budget(None, env={}) == DEFAULT_VERIFICATION_REPAIR_ATTEMPTS
    assert resolve_repair_attempt_budget(None, env={"ALYSIS_VERIFY_REPAIR_ATTEMPTS": "4"}) == 4
    assert resolve_repair_attempt_budget(0, env={"ALYSIS_VERIFY_REPAIR_ATTEMPTS": "4"}) == 0
    assert resolve_repair_attempt_budget(-3, env={}) == 0
    assert resolve_repair_attempt_budget(999, env={}) == 10
    assert (
        resolve_repair_attempt_budget(None, env={"ALYSIS_VERIFY_REPAIR_ATTEMPTS": "nope"})
        == DEFAULT_VERIFICATION_REPAIR_ATTEMPTS
    )


def test_repair_prompt_keeps_the_tail_of_long_command_output() -> None:
    long_output = "noise\n" * 5000 + "FINAL ASSERTION ERROR\n"
    result = VerifyRunResult(
        commands=["pytest -q"],
        command_results=[VerifyCommandResult("pytest -q", 1, long_output)],
        artifact_path=Path("verify.txt"),
    )
    excerpts = verification_failure_excerpts(result, max_chars=200)
    assert len(excerpts) == 1
    command, exit_code, text = excerpts[0]
    assert command == "pytest -q"
    assert exit_code == 1
    assert "FINAL ASSERTION ERROR" in text
    assert "truncated" in text

    prompt = build_repair_instruction(
        base_instruction="ORIGINAL TASK BODY",
        task_id="T01",
        attempt=1,
        max_attempts=2,
        verify_summary="verification failed (0/1)",
        excerpts=excerpts,
        artifact_path="verify.txt",
    )
    assert "FINAL ASSERTION ERROR" in prompt
    assert prompt.endswith("ORIGINAL TASK BODY")
    assert "Do not weaken, skip, or delete the failing checks" in prompt


# --------------------------------------------------------------------------- #
# evidence preservation
# --------------------------------------------------------------------------- #


def test_preserve_evidence_captures_committed_uncommitted_and_untracked_work(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "work"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "feat/t01")
    (repo / "committed.py").write_text("committed = True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "--no-verify", "-m", "wip")
    (repo / "README.md").write_text("hello\nedited but never committed\n", encoding="utf-8")
    (repo / "brand_new.py").write_text("never_committed = 'precious'\n", encoding="utf-8")

    execution_dir = tmp_path / "run" / "execution"
    verify_log = tmp_path / "verify.txt"
    verify_log.write_text("pytest -q\nE   assert False\n", encoding="utf-8")

    evidence = preserve_failed_task_evidence(
        execution_dir=execution_dir,
        task_id="T01",
        worktree_path=repo,
        base_branch="main",
        branch="feat/t01",
        reason="worker failed",
        verification_log_path=verify_log,
        verification_summary="verification failed (0/1)",
    )

    assert evidence.captured
    assert evidence.errors == ()
    patch_text = evidence.patch_path.read_text(encoding="utf-8")
    assert "committed.py" in patch_text
    assert "edited but never committed" in patch_text
    assert "never_committed = 'precious'" in patch_text
    assert "brand_new.py" in evidence.captured_untracked_files

    log_text = evidence.verification_log_path.read_text(encoding="utf-8")
    assert "E   assert False" in log_text

    metadata = json.loads(evidence.metadata_path.read_text(encoding="utf-8"))
    assert metadata["task_id"] == "T01"
    assert metadata["branch"] == "feat/t01"
    assert metadata["reason"] == "worker failed"

    # Evidence lives in the run, not the workspace: destroying the workspace now
    # must not touch it.
    assert evidence.directory == evidence_dir_for(execution_dir, "T01")
    assert not evidence.directory.is_relative_to(repo)


def test_preserve_evidence_reports_a_missing_workspace_without_raising(tmp_path: Path) -> None:
    evidence = preserve_failed_task_evidence(
        execution_dir=tmp_path / "run" / "execution",
        task_id="T02",
        worktree_path=tmp_path / "gone",
        base_branch="main",
        branch="feat/t02",
        verification_summary="verification failed",
    )
    assert evidence.patch_path is None
    assert any("no longer exists" in item for item in evidence.errors)
    # The metadata and log are still written, so the failure itself is on record.
    assert (evidence.directory / METADATA_FILE_NAME).exists()
    assert (evidence.directory / VERIFICATION_LOG_FILE_NAME).exists()


def test_swarm_keeps_unverifiable_task_as_completed_unverified_and_does_not_merge(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Docs only", estimated_files=["docs/a.md"], branch="feat/t01-a")
    save_plan(paths, plan)
    task_id = str(task["id"])
    branch = str(task["branch"])
    worktree_path = paths.run_dir / "worktrees" / task_id / "repo"
    merge_calls: list[str] = []

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worktree = Path(kwargs["worktree_repo_path"])
        return TaskWorkerResult(
            task_id=task_id,
            title="Docs only",
            branch=branch,
            worktree_path=os.fspath(worktree),
            started_at="2026-02-19T00:00:00+00:00",
            finished_at="2026-02-19T00:01:00+00:00",
            success=True,
            summary="worker completed but nothing verified it",
            commit_hash="deadbeef",
            error=None,
            report_path=f".alysis/runs/x/execution/reports/{task_id}.md",
            patch_path=f".alysis/runs/x/execution/patches/{task_id}.diff",
            log_path=f".alysis/runs/x/execution/logs/{task_id}.jsonl",
            log_pointer_path=f".alysis/runs/x/execution/logs/{task_id}.log.json",
            warnings=[],
            changed_files=["docs/a.md"],
            verify_failed=False,
            verification_unavailable=True,
            verify_summary=(
                "verification skipped: no authoritative commands available; "
                "task kept as completed_unverified"
            ),
            verify_artifact_path=None,
        )

    def fake_merge_runner(*_args, **kwargs):  # type: ignore[no-untyped-def]
        merge_calls.append(str(kwargs.get("task_branch") or "merge"))
        return "merge-hash"

    run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=Console(file=io.StringIO()),
        worker_runner=fake_worker_runner,
        merge_runner=fake_merge_runner,
    )

    reloaded = load_plan(paths)
    assert reloaded["tasks"][0]["status"] == TASK_STATUS_COMPLETED_UNVERIFIED
    # Nothing merges without a check behind it...
    assert merge_calls == []
    # ...and nothing is cleaned up either: the work stays where a human can review it.
    assert worktree_path.exists()
    assert _branch_exists(repo, branch)


def test_swarm_writes_failed_task_evidence_before_deleting_the_worktree(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(plan, title="Doomed", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)
    task_id = str(task["id"])
    branch = str(task["branch"])

    def fake_worker_runner(**kwargs):  # type: ignore[no-untyped-def]
        worktree = Path(kwargs["worktree_repo_path"])
        (worktree / "half_finished.py").write_text("value = 'unsaved work'\n", encoding="utf-8")
        return TaskWorkerResult(
            task_id=task_id,
            title="Doomed",
            branch=branch,
            worktree_path=os.fspath(worktree),
            started_at="2026-02-19T00:00:00+00:00",
            finished_at="2026-02-19T00:01:00+00:00",
            success=False,
            summary="worker failed",
            commit_hash=None,
            error="worker failed",
            report_path=f".alysis/runs/x/execution/reports/{task_id}.md",
            patch_path=f".alysis/runs/x/execution/patches/{task_id}.diff",
            log_path=f".alysis/runs/x/execution/logs/{task_id}.jsonl",
            log_pointer_path=f".alysis/runs/x/execution/logs/{task_id}.log.json",
            warnings=[],
            changed_files=[],
            verify_failed=True,
            verify_summary="verification failed (0/1)",
            verify_artifact_path=None,
        )

    worktree_path = paths.run_dir / "worktrees" / task_id / "repo"
    code = run_swarm(
        paths=paths,
        plan=plan,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=False,
        max_steps=10,
        api_key_override="k",
        no_log=False,
        parallel=1,
        base_branch="main",
        max_tasks=None,
        max_attempts=None,
        dry_run=False,
        keep_worktrees=False,
        retry_failed=False,
        retry_changes_requested=False,
        only=None,
        retry_merge_conflicts=False,
        review=False,
        console=Console(file=io.StringIO()),
        worker_runner=fake_worker_runner,
        merge_runner=lambda *_a, **_k: "merge-hash",
    )

    assert code == 1
    # Cleanup did run...
    assert not worktree_path.exists()
    # ...but the uncommitted work it removed is recorded in the run's artifacts.
    evidence_dir = evidence_dir_for(paths.execution_dir, task_id)
    assert evidence_dir.is_dir()
    patch_text = (evidence_dir / PATCH_FILE_NAME).read_text(encoding="utf-8")
    assert "half_finished.py" in patch_text
    assert "value = 'unsaved work'" in patch_text
    assert (evidence_dir / VERIFICATION_LOG_FILE_NAME).exists()
    metadata = json.loads((evidence_dir / METADATA_FILE_NAME).read_text(encoding="utf-8"))
    assert metadata["branch"] == branch
    assert metadata["extra"]["keep_worktrees"] is False
