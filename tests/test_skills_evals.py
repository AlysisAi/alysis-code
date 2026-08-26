from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import alysis_code.skills.eval_runner as eval_runner_module
from alysis_code.config import AppConfig
from alysis_code.skills.eval_models import (
    SkillsEvalCase,
    SkillsEvalExecutionRequest,
    SkillsEvalExecutionResult,
    SkillsEvalMode,
    SkillsEvalRecord,
    SkillsEvalVerificationResult,
)
from alysis_code.skills.eval_runner import (
    OneShotSkillsEvalExecutor,
    classify_skills_eval_failure,
    run_skills_eval_auth_preflight,
)
from alysis_code.skills.evals import (
    aggregate_skills_eval_records,
    evaluate_skills_launch_readiness,
    extract_skills_eval_metrics,
    load_skills_eval_cases,
    prepared_skills_eval_workspace,
    render_skills_eval_summary_markdown,
    resolve_skills_eval_modes,
    run_skills_eval_suite,
    summarize_skills_launch_candidate_metrics,
)
from alysis_code.skills.models import SkillBundle
from alysis_code.skills.prompting import EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS


class _FakeExecutor:
    def __init__(self) -> None:
        self.requests: list[SkillsEvalExecutionRequest] = []

    def execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        self.requests.append(request)
        session_log_path = request.sessions_dir / f"{request.session_id}.jsonl"
        session_log_path.parent.mkdir(parents=True, exist_ok=True)
        session_log_path.write_text("", encoding="utf-8")

        conventions_present = any(
            (request.workspace / filename).exists()
            for filename in ("AGENTS.md", "CLAUDE.md", "CONVENTIONS.md")
        )
        skills_root_exists = (request.workspace / ".alysis_skills").exists()
        matched_skill_names: tuple[str, ...] = ()
        skill_read_names: tuple[str, ...] = ()
        explicit_used = False
        if request.case.invocation_mode == "explicit_skill":
            explicit_used = True
        elif request.mode.skills_enabled and request.case.expected_skills:
            skill_read_names = (request.case.expected_skills[0],)

        tool_call_count = len(skill_read_names)
        return SkillsEvalExecutionResult(
            agent_exit_code=0,
            skills_advertised_present=request.mode.skills_enabled and skills_root_exists,
            repo_conventions_present=conventions_present,
            matched_skill_context_attached=bool(matched_skill_names),
            matched_skill_names=matched_skill_names,
            explicit_skill_context_used=explicit_used,
            skill_read_called=bool(skill_read_names),
            skill_read_names=skill_read_names,
            skill_read_call_count=len(skill_read_names),
            tool_call_count=tool_call_count,
            session_log_path=session_log_path,
            session_artifact_root=request.sessions_dir / request.session_id,
        )


def _fake_verification_runner(*, workspace: Path, command: str) -> SkillsEvalVerificationResult:
    _ = workspace, command
    return SkillsEvalVerificationResult(exit_code=0, output_preview="ok")


class _ExecutorFakeStore:
    def __init__(self, path: Path) -> None:
        self.enabled = True
        self.path = path
        self.session_artifact_root = path.parent / "artifacts"


class _ExecutorFakeSession:
    def __init__(self, tmp_path: Path, skill: SkillBundle) -> None:
        self.messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "<skill_context>\n- pytest\n</skill_context>\n"},
            {"role": "user", "content": "<repo_conventions>\nrepo rules\n</repo_conventions>\n"},
        ]
        self.skill_registry = {"pytest": skill}
        self.store = _ExecutorFakeStore(tmp_path / "sessions" / "skills-eval.jsonl")
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "tool_call",
                            "payload": {"name": "skill_read", "arguments": {"name": "pytest"}},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "tool_call",
                            "payload": {"name": "fs_read", "arguments": {"path": "README.md"}},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.run_turn_calls: list[dict[str, Any]] = []
        self.closed = False

    def run_turn(self, instruction: str, **kwargs: Any) -> int:
        self.run_turn_calls.append({"instruction": instruction, **kwargs})
        return 0

    def close(self) -> None:
        self.closed = True


def test_load_skills_eval_cases_parses_sample_fixture_manifest() -> None:
    manifest = Path("tests/fixtures/skills_eval/sample_cases.json").resolve()

    cases = load_skills_eval_cases(manifest)

    assert len(cases) == 5
    assert cases[0].id == "conventions_parser_tests"
    assert cases[0].workspace.is_dir()
    explicit = next(case for case in cases if case.invocation_mode == "explicit_skill")
    assert explicit.explicit_skill_name == "pytest"
    assert explicit.expected_skills == ("pytest",)
    assert any(case.id == "empty_workspace_first_skill_authoring" for case in cases)


def test_resolve_skills_eval_modes_returns_expected_matrix() -> None:
    modes = resolve_skills_eval_modes()

    assert [mode.name for mode in modes] == [
        "baseline",
        "conventions_only",
        "skills_manual_only",
        "skills_auto_only",
        "combined_manual",
        "combined_auto",
    ]
    assert modes[0].skills_enabled is False and modes[0].conventions_enabled is False
    assert modes[-1].skills_enabled is True and modes[-1].skills_auto_invoke is True


def test_prepared_eval_workspace_uses_explicit_external_temp_base(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "README.md").write_text("hi\n", encoding="utf-8")
    temp_base = tmp_path / "external-temp"
    mode = SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )

    with prepared_skills_eval_workspace(
        source_workspace=source,
        mode=mode,
        temp_base_dir=temp_base,
    ) as workspace:
        assert workspace.exists()
        assert workspace.resolve().is_relative_to(temp_base.resolve())
        assert not workspace.resolve().is_relative_to(source.resolve())
        assert workspace.name == "workspace"
        assert workspace.parent.name.startswith("se-")
        preserved_workspace = workspace

    assert temp_base.exists()
    assert not preserved_workspace.exists()


def test_prepared_eval_workspace_masks_conventions_when_mode_disables_them(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace"
    (source / "nested").mkdir(parents=True)
    (source / "src").mkdir()
    (source / ".git").mkdir()
    (source / ".alysis_skills" / "pytest").mkdir(parents=True)
    (source / ".agents" / "skills" / "lint").mkdir(parents=True)
    (source / ".claude" / "skills" / "docs").mkdir(parents=True)
    (source / ".github" / "skills" / "review").mkdir(parents=True)
    (source / "AGENTS.md").write_text("repo conventions\n", encoding="utf-8")
    (source / "nested" / "CLAUDE.md").write_text("nested conventions\n", encoding="utf-8")
    (source / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / ".alysis_skills" / "pytest" / "SKILL.md").write_text(
        "pytest skill\n",
        encoding="utf-8",
    )
    (source / ".agents" / "skills" / "lint" / "SKILL.md").write_text(
        "lint skill\n",
        encoding="utf-8",
    )
    (source / ".agents" / "notes.md").write_text("keep me\n", encoding="utf-8")
    (source / ".claude" / "skills" / "docs" / "SKILL.md").write_text(
        "docs skill\n",
        encoding="utf-8",
    )
    (source / ".claude" / "workspace-notes.md").write_text("keep me\n", encoding="utf-8")
    (source / ".github" / "skills" / "review" / "SKILL.md").write_text(
        "review skill\n",
        encoding="utf-8",
    )

    baseline = SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )
    conventions_only = SkillsEvalMode(
        name="conventions_only",
        conventions_enabled=True,
        skills_enabled=False,
        skills_auto_invoke=False,
    )
    combined_manual = SkillsEvalMode(
        name="combined_manual",
        conventions_enabled=True,
        skills_enabled=True,
        skills_auto_invoke=False,
    )

    with prepared_skills_eval_workspace(source_workspace=source, mode=baseline) as workspace:
        masked_workspace = workspace
        assert not (workspace / "AGENTS.md").exists()
        assert not (workspace / "nested" / "CLAUDE.md").exists()
        assert not (workspace / ".git").exists()
        assert not (workspace / ".alysis_skills").exists()
        assert not (workspace / ".agents" / "skills").exists()
        assert not (workspace / ".claude" / "skills").exists()
        assert not (workspace / ".github" / "skills").exists()
        assert (workspace / ".agents" / "notes.md").read_text(encoding="utf-8") == "keep me\n"
        assert (workspace / ".claude" / "workspace-notes.md").read_text(
            encoding="utf-8"
        ) == "keep me\n"
        assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"

    assert not masked_workspace.exists()

    with prepared_skills_eval_workspace(
        source_workspace=source,
        mode=conventions_only,
    ) as workspace:
        assert (workspace / "AGENTS.md").exists()
        assert (workspace / "nested" / "CLAUDE.md").exists()
        assert not (workspace / ".git").exists()
        assert not (workspace / ".alysis_skills").exists()
        assert not (workspace / ".agents" / "skills").exists()
        assert not (workspace / ".claude" / "skills").exists()
        assert not (workspace / ".github" / "skills").exists()
        assert (workspace / ".agents" / "notes.md").exists()
        assert (workspace / ".claude" / "workspace-notes.md").exists()

    with prepared_skills_eval_workspace(
        source_workspace=source,
        mode=combined_manual,
    ) as workspace:
        assert (workspace / "AGENTS.md").exists()
        assert (workspace / ".alysis_skills" / "pytest" / "SKILL.md").exists()
        assert (workspace / ".agents" / "skills" / "lint" / "SKILL.md").exists()
        assert (workspace / ".claude" / "skills" / "docs" / "SKILL.md").exists()
        assert (workspace / ".github" / "skills" / "review" / "SKILL.md").exists()
        assert not (workspace / ".git").exists()


def test_prepared_eval_workspace_ignores_local_worktrees_and_reports(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    (source / ".claude" / "worktrees" / "agent-clone" / "src").mkdir(parents=True)
    (source / ".claude" / "workspace-notes.md").write_text("keep me\n", encoding="utf-8")
    (source / ".claude" / "worktrees" / "agent-clone" / "src" / "dup.py").write_text(
        "print('dup')\n",
        encoding="utf-8",
    )
    (source / "reports" / "old-run").mkdir(parents=True)
    (source / "reports" / "old-run" / "summary.json").write_text("{}", encoding="utf-8")
    (source / ".mypy_cache").mkdir()
    (source / ".mypy_cache" / "cache.json").write_text("{}", encoding="utf-8")
    (source / "reports_eval").mkdir()
    (source / "reports_eval" / "notes.md").write_text("keep reports_eval\n", encoding="utf-8")
    (source / "README.md").write_text("hi\n", encoding="utf-8")

    mode = SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )

    with prepared_skills_eval_workspace(source_workspace=source, mode=mode) as workspace:
        assert (workspace / "README.md").read_text(encoding="utf-8") == "hi\n"
        assert (workspace / ".claude" / "workspace-notes.md").read_text(
            encoding="utf-8"
        ) == "keep me\n"
        assert not (workspace / ".claude" / "worktrees").exists()
        assert not (workspace / "reports").exists()
        assert not (workspace / ".mypy_cache").exists()
        assert (workspace / "reports_eval" / "notes.md").read_text(
            encoding="utf-8"
        ) == "keep reports_eval\n"


def test_extract_skills_eval_metrics_reads_host_events() -> None:
    events = [
        {
            "type": "skill_matches",
            "payload": {
                "matches": [
                    {"name": "pytest"},
                    {"name": "pytest"},
                    {"name": "lint"},
                ]
            },
        },
        {
            "type": "tool_call",
            "payload": {"name": "skill_read", "arguments": {"name": "pytest"}},
        },
        {
            "type": "tool_call",
            "payload": {"name": "fs_read", "arguments": {"path": "README.md"}},
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["matched_skill_context_attached"] is True
    assert metrics["matched_skill_names"] == ("pytest", "lint")
    assert metrics["skill_read_called"] is True
    assert metrics["skill_read_names"] == ("pytest",)
    assert metrics["skill_read_call_count"] == 1
    assert metrics["manual_skill_bundle_accessed"] is False
    assert metrics["manual_skill_bundle_names"] == ()
    assert metrics["manual_skill_bundle_access_count"] == 0
    assert metrics["tool_call_count"] == 2


def test_extract_skills_eval_metrics_tracks_skill_lifecycle_cli_usage() -> None:
    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "shell_run",
                "arguments": {"cmd": "alysis skill init pytest-debug"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "verify_run",
                "arguments": {
                    "commands": [
                        "alysis skill validate ./.alysis_skills/pytest-debug",
                        "pytest -q",
                    ]
                },
            },
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["skill_lifecycle_cli_used"] is True
    assert metrics["skill_lifecycle_cli_commands"] == ("init", "validate")
    assert metrics["skill_lifecycle_cli_call_count"] == 2
    assert metrics["tool_call_count"] == 2


def test_extract_skills_eval_metrics_tracks_direct_manual_skill_access(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".alysis_skills" / "pytest").mkdir(parents=True)
    (workspace / ".agents" / "skills" / "lint").mkdir(parents=True)
    (workspace / ".claude" / "skills" / "docs").mkdir(parents=True)
    (workspace / ".github" / "skills" / "review").mkdir(parents=True)

    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_read",
                "arguments": {"path": ".alysis_skills/pytest/SKILL.md"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_read_lines",
                "arguments": {
                    "path": str(workspace / ".agents" / "skills" / "lint" / "SKILL.md"),
                    "start_line": 1,
                    "end_line": 20,
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_list",
                "arguments": {"root_path": ".claude/skills/docs"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "search_rg",
                "arguments": {"pattern": "pytest", "root_path": ".github/skills/review"},
            },
        },
        {
            "type": "tool_call",
            "payload": {"name": "fs_read", "arguments": {"path": "README.md"}},
        },
        {
            "type": "tool_call",
            "payload": {"name": "search_rg", "arguments": {"pattern": "pytest", "root_path": "."}},
        },
    ]

    metrics = extract_skills_eval_metrics(events, workspace_root=workspace)

    assert metrics["manual_skill_bundle_accessed"] is True
    assert metrics["manual_skill_bundle_names"] == ("pytest", "lint", "docs", "review")
    assert metrics["manual_skill_bundle_access_count"] == 4
    assert metrics["tool_call_count"] == 6


def test_extract_skills_eval_metrics_tracks_launch_runtime_events() -> None:
    events = [
        {
            "type": "route_decision",
            "payload": {
                "route": "repo",
                "execution_posture_source": "fallback",
                "router_execution_posture_source": "router",
            },
        },
        {
            "type": "interactive_completion_gate_failed",
            "payload": {
                "stage": "verification_incomplete",
                "state": {"verification_attempt_count": 1},
            },
        },
        {
            "type": "interactive_completion_gate_incomplete_after_retries",
            "payload": {
                "stage": "verification_not_attempted",
                "state": {"verification_attempt_count": 2},
            },
        },
        {
            "type": "forced_final_summary_requested",
            "payload": {"reason": "step_budget"},
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["completion_gate_failure_count"] == 1
    assert metrics["completion_gate_incomplete_after_retries_count"] == 1
    assert metrics["forced_final_summary_count"] == 1
    assert metrics["verification_credit_miss_count"] == 2
    assert "execution_posture_fallback_count" not in metrics


def test_extract_skills_eval_metrics_counts_one_shot_launch_runtime_events() -> None:
    events = [
        {
            "type": "one_shot_completion_gate_failed",
            "payload": {
                "stage": "verification_not_attempted",
                "state": {"verification_attempt_count": 1},
            },
        },
        {
            "type": "one_shot_completion_gate_incomplete_after_retries",
            "payload": {
                "stage": "verification_incomplete",
                "state": {"verification_attempt_count": 3},
            },
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["completion_gate_failure_count"] == 1
    assert metrics["completion_gate_incomplete_after_retries_count"] == 1
    assert metrics["verification_credit_miss_count"] == 2


def test_aggregate_skills_eval_records_computes_mode_rates() -> None:
    records = [
        SkillsEvalRecord(
            case_id="a",
            mode="combined_auto",
            workspace=Path("/tmp/a"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("pytest",),
            tags=(),
            notes="",
            status="passed",
            passed=True,
            skip_reason=None,
            agent_exit_code=0,
            verification_command=None,
            verification_exit_code=None,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=True,
            manual_skill_bundle_names=("pytest",),
            manual_skill_bundle_access_count=1,
            tool_call_count=0,
            completion_gate_failure_count=1,
            completion_gate_incomplete_after_retries_count=0,
            forced_final_summary_count=0,
            verification_credit_miss_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="b",
            mode="combined_auto",
            workspace=Path("/tmp/b"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=(),
            tags=(),
            notes="",
            status="failed",
            passed=False,
            skip_reason=None,
            agent_exit_code=1,
            verification_command=None,
            verification_exit_code=None,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=True,
            manual_skill_bundle_names=("lint",),
            manual_skill_bundle_access_count=1,
            tool_call_count=0,
            completion_gate_failure_count=0,
            completion_gate_incomplete_after_retries_count=1,
            forced_final_summary_count=1,
            verification_credit_miss_count=0,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="c",
            mode="baseline",
            workspace=Path("/tmp/c"),
            task="task",
            invocation_mode="explicit_skill",
            explicit_skill_name="pytest",
            expected_skills=("pytest",),
            tags=(),
            notes="",
            status="skipped",
            passed=None,
            skip_reason="explicit skill invocation requires a skills-enabled mode",
            agent_exit_code=None,
            verification_command=None,
            verification_exit_code=None,
            verification_output_preview=None,
            skills_advertised_present=False,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=0,
            session_log_path=None,
            session_artifact_root=None,
        ),
    ]

    summary = aggregate_skills_eval_records(records)
    combined_auto = summary["modes"]["combined_auto"]
    baseline = summary["modes"]["baseline"]

    assert combined_auto["executed_runs"] == 2
    assert combined_auto["pass_rate"] == 0.5
    assert combined_auto["skill_trigger_rate"] == 1.0
    assert combined_auto["skill_lifecycle_cli_rate"] == 0.0
    assert combined_auto["manual_skill_access_rate"] == 1.0
    assert combined_auto["false_positive_rate"] == 1.0
    assert combined_auto["completion_gate_failure_rate"] == 0.5
    assert combined_auto["completion_gate_incomplete_after_retries_rate"] == 0.5
    assert combined_auto["forced_final_summary_rate"] == 0.5
    assert combined_auto["verification_credit_miss_rate"] == 0.5
    assert baseline["skipped_runs"] == 1
    assert summary["relevant_skill_usage_rate"] == 1.0
    assert summary["explicit_invocation_success_rate"] is None
    assert summary["completion_gate_failure_rate"] == 0.5
    assert summary["forced_final_summary_rate"] == 0.5
    assert summary["per_skill"]["pytest"]["runs"] == 1
    assert summary["per_skill"]["pytest"]["triggered"] == 1


def test_launch_candidate_metrics_ignore_control_mode_denominator_pollution() -> None:
    records = [
        SkillsEvalRecord(
            case_id="baseline_expected",
            mode="baseline",
            workspace=Path("/tmp/baseline"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("pytest-debug",),
            tags=(),
            notes="",
            status="failed",
            passed=False,
            skip_reason=None,
            agent_exit_code=1,
            verification_command=None,
            verification_exit_code=1,
            verification_output_preview=None,
            skills_advertised_present=False,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="conventions_expected",
            mode="conventions_only",
            workspace=Path("/tmp/conventions"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("architecture-review",),
            tags=(),
            notes="",
            status="failed",
            passed=False,
            skip_reason=None,
            agent_exit_code=1,
            verification_command=None,
            verification_exit_code=1,
            verification_output_preview=None,
            skills_advertised_present=False,
            repo_conventions_present=True,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="manual_explicit",
            mode="skills_manual_only",
            workspace=Path("/tmp/manual"),
            task="task",
            invocation_mode="explicit_skill",
            explicit_skill_name="pytest-debug",
            expected_skills=("pytest-debug",),
            tags=(),
            notes="",
            status="passed",
            passed=True,
            skip_reason=None,
            agent_exit_code=0,
            verification_command=None,
            verification_exit_code=0,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=True,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="combined_manual_expected",
            mode="combined_manual",
            workspace=Path("/tmp/combined"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("verification-playbook",),
            tags=(),
            notes="",
            status="passed",
            passed=True,
            skip_reason=None,
            agent_exit_code=0,
            verification_command=None,
            verification_exit_code=0,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=True,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=True,
            skill_read_names=("verification-playbook",),
            skill_read_call_count=1,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=2,
            session_log_path=None,
            session_artifact_root=None,
        ),
    ]

    summary = aggregate_skills_eval_records(records)
    launch = summarize_skills_launch_candidate_metrics(
        summary,
        config_snapshot={"skills_auto_invoke": False},
    )
    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": False},
    )

    assert summary["relevant_skill_usage_rate_all_modes"] == 0.5
    assert summary["relevant_skill_usage_rate_skill_enabled"] == 1.0
    assert summary["pass_rate_all_modes"] == 0.5
    assert summary["pass_rate_skill_enabled"] == 1.0
    assert launch["launch_mode_names"] == ["skills_manual_only", "combined_manual"]
    assert launch["relevant_skill_usage_rate_launch_modes"] == 1.0
    assert launch["pass_rate_launch_modes"] == 1.0
    assert gates["production_ready"] is True


def test_summarize_skills_launch_candidate_metrics_uses_auto_modes_for_default_true() -> None:
    summary = {
        "modes": {
            "skills_manual_only": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 0,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 0,
                "explicit_skill_runs": 0,
                "explicit_skill_success_count": 0,
            },
            "combined_manual": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 0,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 0,
                "explicit_skill_runs": 0,
                "explicit_skill_success_count": 0,
            },
            "skills_auto_only": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 2,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 2,
                "explicit_skill_runs": 1,
                "explicit_skill_success_count": 1,
            },
            "combined_auto": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 2,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 2,
                "explicit_skill_runs": 1,
                "explicit_skill_success_count": 1,
            },
        }
    }

    launch = summarize_skills_launch_candidate_metrics(
        summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert launch["launch_mode_basis"] == "skills_auto_invoke=true"
    assert launch["launch_mode_names"] == ["skills_auto_only", "combined_auto"]
    assert launch["pass_rate_launch_modes"] == 1.0
    assert launch["relevant_skill_usage_rate_launch_modes"] == 1.0
    assert launch["explicit_invocation_success_rate_launch_modes"] == 1.0


def test_evaluate_skills_launch_readiness_fails_when_runtime_gates_are_exceeded() -> None:
    summary = {
        "pass_rate": 0.10,
        "completion_gate_failure_rate": 0.25,
        "completion_gate_incomplete_after_retries_rate": 0.10,
        "forced_final_summary_rate": 0.20,
        "verification_credit_miss_rate": 0.30,
        "relevant_skill_usage_rate": 0.70,
        "explicit_invocation_success_rate": 0.50,
    }

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": False},
    )

    assert gates["production_ready"] is False
    assert sorted(gates["failing_gates"]) == [
        "completion_gate_failure_rate",
        "completion_gate_incomplete_after_retries_rate",
        "explicit_skill_success_rate",
        "forced_final_summary_rate",
        "pass_rate",
        "relevant_skill_usage_rate",
        "verification_credit_miss_rate",
    ]


def test_evaluate_skills_launch_readiness_allows_default_auto_invoke_enabled() -> None:
    summary = {
        "pass_rate": 1.0,
        "completion_gate_failure_rate": 0.0,
        "completion_gate_incomplete_after_retries_rate": 0.0,
        "forced_final_summary_rate": 0.0,
        "verification_credit_miss_rate": 0.0,
        "relevant_skill_usage_rate": 1.0,
        "explicit_invocation_success_rate": 1.0,
    }

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert gates["production_ready"] is True
    assert gates["failing_gates"] == []
    assert "skills_auto_invoke_default_disabled" not in gates["gates"]


def test_evaluate_skills_launch_readiness_passes_only_when_thresholds_are_met() -> None:
    summary = {
        "pass_rate": 1.0,
        "completion_gate_failure_rate": 0.0,
        "completion_gate_incomplete_after_retries_rate": 0.0,
        "forced_final_summary_rate": 0.0,
        "verification_credit_miss_rate": 0.0,
        "relevant_skill_usage_rate": 1.0,
        "explicit_invocation_success_rate": 1.0,
    }

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": False},
    )

    assert gates["production_ready"] is True
    assert gates["failing_gates"] == []


def test_render_skills_eval_summary_markdown_includes_launch_metrics_and_release_gates() -> None:
    summary = {
        "generated_at": "2026-04-24T00:00:00+00:00",
        "modes": {
            "combined_auto": {
                "executed_runs": 2,
                "skipped_runs": 0,
                "pass_rate": 1.0,
                "skill_trigger_rate": 1.0,
                "skill_read_rate": 0.5,
                "skill_lifecycle_cli_rate": 0.0,
                "manual_skill_access_rate": 0.5,
                "false_negative_rate": 0.0,
                "false_positive_rate": 0.0,
                "explicit_invocation_success_rate": None,
            }
        },
        "completion_gate_failure_count": 1,
        "completion_gate_failure_run_count": 1,
        "completion_gate_failure_rate": 0.5,
        "completion_gate_incomplete_after_retries_count": 0,
        "completion_gate_incomplete_after_retries_run_count": 0,
        "completion_gate_incomplete_after_retries_rate": 0.0,
        "forced_final_summary_count": 1,
        "forced_final_summary_run_count": 1,
        "forced_final_summary_rate": 0.5,
        "verification_credit_miss_count": 1,
        "verification_credit_miss_run_count": 1,
        "verification_credit_miss_rate": 0.5,
        "pass_rate": 0.5,
        "relevant_skill_usage_rate": 1.0,
        "explicit_invocation_success_rate": 1.0,
        "release_gates": {
            "gates": {
                "completion_gate_failure_rate": {
                    "status": "fail",
                    "actual": 0.5,
                    "max_allowed": 0.05,
                },
            }
        },
    }

    rendered = render_skills_eval_summary_markdown(summary=summary)

    assert "Launch Runtime Metrics" in rendered
    assert "launch-candidate pass rate" in rendered
    assert "relevant skill usage rate (launch-candidate modes)" in rendered
    assert "completion-gate failure rate" in rendered
    assert "Release Gates" in rendered
    assert "`completion_gate_failure_rate`: fail" in rendered


def test_skills_eval_record_manual_access_counts_as_skill_activity() -> None:
    record = SkillsEvalRecord(
        case_id="manual",
        mode="combined_manual",
        workspace=Path("/tmp/manual"),
        task="task",
        invocation_mode="normal",
        explicit_skill_name=None,
        expected_skills=("pytest",),
        tags=(),
        notes="",
        status="passed",
        passed=True,
        skip_reason=None,
        agent_exit_code=0,
        verification_command=None,
        verification_exit_code=None,
        verification_output_preview=None,
        skills_advertised_present=True,
        repo_conventions_present=False,
        matched_skill_context_attached=False,
        matched_skill_names=(),
        explicit_skill_context_used=False,
        skill_read_called=False,
        skill_read_names=(),
        skill_read_call_count=0,
        manual_skill_bundle_accessed=True,
        manual_skill_bundle_names=("pytest",),
        manual_skill_bundle_access_count=1,
        tool_call_count=1,
        session_log_path=None,
        session_artifact_root=None,
    )

    assert record.observed_skill_names() == ("pytest",)
    assert record.relevant_skill_used() is True
    assert record.any_skill_activity() is True


def test_run_skills_eval_suite_writes_results_and_skips_explicit_cases_without_skills(
    tmp_path: Path,
) -> None:
    manifest = Path("tests/fixtures/skills_eval/sample_cases.json").resolve()
    cases = load_skills_eval_cases(manifest)
    selected_cases = tuple(
        case
        for case in cases
        if case.id in {"skills_parser_pytest_normal", "skills_parser_pytest_explicit"}
    )
    selected_modes = resolve_skills_eval_modes(["baseline", "skills_manual_only", "combined_auto"])
    executor = _FakeExecutor()

    artifacts = run_skills_eval_suite(
        cases=selected_cases,
        modes=selected_modes,
        output_dir=tmp_path / "out",
        executor=executor,
        verification_runner=_fake_verification_runner,
        manifest_path=manifest,
        max_steps=7,
    )

    assert artifacts.results_path.exists()
    assert artifacts.summary_json_path.exists()
    assert artifacts.summary_md_path.exists()
    results = [
        json.loads(line)
        for line in artifacts.results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(results) == 6
    skipped = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_explicit" and item["mode"] == "baseline"
    ]
    assert skipped and skipped[0]["status"] == "skipped"
    assert skipped[0]["skip_reason"] == "explicit skill invocation requires a skills-enabled mode"

    manual_normal = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_normal" and item["mode"] == "skills_manual_only"
    ][0]
    auto_normal = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_normal" and item["mode"] == "combined_auto"
    ][0]
    explicit_auto = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_explicit" and item["mode"] == "combined_auto"
    ][0]

    assert manual_normal["skill_read_called"] is True
    assert "manual_skill_bundle_accessed" in manual_normal
    assert "manual_skill_bundle_names" in manual_normal
    assert "manual_skill_bundle_access_count" in manual_normal
    assert auto_normal["matched_skill_context_attached"] is False
    assert auto_normal["skill_read_called"] is True
    assert explicit_auto["explicit_skill_context_used"] is True
    summary_markdown = artifacts.summary_md_path.read_text(encoding="utf-8")
    assert "skills_manual_only" in summary_markdown
    assert "lifecycle CLI rate" in summary_markdown
    assert "manual access rate" in summary_markdown

    assert executor.requests
    baseline_requests = [
        request for request in executor.requests if request.mode.name == "baseline"
    ]
    manual_requests = [
        request for request in executor.requests if request.mode.name == "skills_manual_only"
    ]
    assert baseline_requests
    assert not (baseline_requests[0].workspace / "AGENTS.md").exists()
    assert manual_requests and not (manual_requests[0].workspace / "AGENTS.md").exists()


def test_one_shot_skills_eval_executor_uses_explicit_skill_context(
    monkeypatch, tmp_path: Path
) -> None:
    skill = SkillBundle(
        name="pytest",
        description="Debug pytest failures.",
        instructions='Read "$ARGUMENTS" starting with $1 and $2.',
        bundle_name="pytest",
        bundle_path=tmp_path / ".alysis_skills" / "pytest",
        entry_path=tmp_path / ".alysis_skills" / "pytest" / "SKILL.md",
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=tmp_path / ".alysis_skills" / "pytest",
        trust_level="untrusted",
    )
    fake_session = _ExecutorFakeSession(tmp_path, skill)

    def _fake_create_session(**kwargs: Any) -> _ExecutorFakeSession:
        _ = kwargs
        return fake_session

    monkeypatch.setattr(
        "alysis_code.skills.eval_runner.create_session",
        _fake_create_session,
    )
    executor = OneShotSkillsEvalExecutor(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        api_key_override="key",
    )
    request = SkillsEvalExecutionRequest(
        case=SkillsEvalCase(
            id="explicit",
            workspace=tmp_path,
            task="Investigate the pytest failure.",
            invocation_mode="explicit_skill",
            explicit_skill_name="pytest",
        ),
        mode=SkillsEvalMode(
            name="combined_manual",
            conventions_enabled=True,
            skills_enabled=True,
            skills_auto_invoke=False,
        ),
        workspace=tmp_path,
        output_dir=tmp_path / "out",
        sessions_dir=tmp_path / "sessions",
        session_id="skills_eval_explicit",
        max_steps=5,
    )

    result = executor.execute(request)

    assert result.agent_exit_code == 0
    assert result.skills_advertised_present is True
    assert result.repo_conventions_present is True
    assert result.explicit_skill_context_used is True
    assert result.skill_read_called is True
    assert result.skill_read_names == ("pytest",)
    assert result.skill_lifecycle_cli_used is False
    assert result.skill_lifecycle_cli_commands == ()
    assert result.skill_lifecycle_cli_call_count == 0
    assert fake_session.run_turn_calls
    explicit_message = fake_session.run_turn_calls[0]["ephemeral_user_messages"][0]
    assert "<explicit_skill_context>" in explicit_message
    assert (
        "turn_requirement: Apply this selected skill before taking other actions on the next user task."
        in explicit_message
    )
    assert (
        "task_binding: Treat this wrapper and the next user message as one bound instruction set."
        in explicit_message
    )
    assert len(explicit_message) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
    assert '- $ARGUMENTS = "Investigate the pytest failure."' in explicit_message
    assert '- $1 = "Investigate"' in explicit_message
    assert '- $2 = "the"' in explicit_message
    assert (
        'Read "Investigate the pytest failure." starting with Investigate and the.'
        in explicit_message
    )
    assert fake_session.closed is True


def test_run_skills_eval_auth_preflight_classifies_auth_failures(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _fake_execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        _ = self, request
        return SkillsEvalExecutionResult(
            agent_exit_code=1,
            error="401 invalid_api_key",
        )

    monkeypatch.setattr(
        OneShotSkillsEvalExecutor,
        "execute",
        _fake_execute,
    )

    result = run_skills_eval_auth_preflight(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        workspace=workspace,
        output_dir=tmp_path / "out",
        api_key_override="test-key",
    )

    assert result.ok is False
    assert result.classification == "auth"
    assert "invalid_api_key" in result.message


def test_run_skills_eval_auth_preflight_succeeds_on_clean_execution(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_log_path = tmp_path / "out" / "sessions" / "preflight.jsonl"

    def _fake_execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        _ = self, request
        session_log_path.parent.mkdir(parents=True, exist_ok=True)
        session_log_path.write_text("", encoding="utf-8")
        return SkillsEvalExecutionResult(
            agent_exit_code=0,
            session_log_path=session_log_path,
            session_artifact_root=session_log_path.parent / "artifacts",
        )

    monkeypatch.setattr(
        OneShotSkillsEvalExecutor,
        "execute",
        _fake_execute,
    )

    result = run_skills_eval_auth_preflight(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        workspace=workspace,
        output_dir=tmp_path / "out",
        api_key_override="test-key",
    )

    assert result.ok is True
    assert result.classification == "ok"
    assert result.message == "Auth preflight passed."
    assert result.session_log_path == session_log_path


def test_one_shot_eval_executor_passes_case_verification_as_authoritative(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, Any] = {}
    session_log_path = tmp_path / "session.jsonl"

    class _FakeStore:
        enabled = True
        path = session_log_path
        session_artifact_root = tmp_path / "artifacts"

    class _FakeSession:
        messages: list[dict[str, object]] = []
        skill_registry: dict[str, object] = {}
        store = _FakeStore()

        def run_turn(self, *_args, **_kwargs) -> int:
            session_log_path.write_text("", encoding="utf-8")
            return 0

        def close(self) -> None:
            return None

    def _fake_create_session(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(eval_runner_module, "create_session", _fake_create_session)
    monkeypatch.setattr(eval_runner_module, "read_session_events", lambda _path: [])
    executor = OneShotSkillsEvalExecutor(cfg=AppConfig(model="test-model"), api_key_override="key")
    request = SkillsEvalExecutionRequest(
        case=SkillsEvalCase(
            id="case",
            workspace=tmp_path,
            task="Write a file and verify it.",
            verification_command="test -f README.md",
        ),
        mode=SkillsEvalMode(
            name="skills_manual_only",
            conventions_enabled=False,
            skills_enabled=True,
            skills_auto_invoke=False,
        ),
        workspace=tmp_path,
        output_dir=tmp_path,
        sessions_dir=tmp_path,
        session_id="session",
        max_steps=3,
    )

    result = executor.execute(request)

    assert result.agent_exit_code == 0
    assert captured["authoritative_verification_commands"] == ["test -f README.md"]


def test_classify_skills_eval_failure_treats_missing_model_as_provider_misconfiguration() -> None:
    classification = classify_skills_eval_failure(
        "Model is not set. Run: alysis config set model <MODEL>",
        agent_exit_code=1,
    )

    assert classification == "provider"
