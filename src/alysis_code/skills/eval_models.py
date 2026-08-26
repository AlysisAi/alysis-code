from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SkillsEvalInvocationMode = Literal["normal", "explicit_skill"]
SkillsEvalRunStatus = Literal["passed", "failed", "skipped"]
SkillsEvalPreflightClassification = Literal[
    "ok",
    "auth",
    "rate_limit",
    "provider",
    "runtime",
]


@dataclass(frozen=True)
class SkillsEvalCase:
    id: str
    workspace: Path
    task: str
    invocation_mode: SkillsEvalInvocationMode = "normal"
    explicit_skill_name: str | None = None
    expected_skills: tuple[str, ...] = ()
    verification_command: str | None = None
    tags: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class SkillsEvalMode:
    name: str
    conventions_enabled: bool
    skills_enabled: bool
    skills_auto_invoke: bool


@dataclass(frozen=True)
class SkillsEvalExecutionRequest:
    case: SkillsEvalCase
    mode: SkillsEvalMode
    workspace: Path
    output_dir: Path
    sessions_dir: Path
    session_id: str
    max_steps: int


@dataclass(frozen=True)
class SkillsEvalExecutionResult:
    agent_exit_code: int | None
    skills_advertised_present: bool = False
    repo_conventions_present: bool = False
    matched_skill_context_attached: bool = False
    matched_skill_names: tuple[str, ...] = ()
    explicit_skill_context_used: bool = False
    skill_read_called: bool = False
    skill_read_names: tuple[str, ...] = ()
    skill_read_call_count: int = 0
    skill_lifecycle_cli_used: bool = False
    skill_lifecycle_cli_commands: tuple[str, ...] = ()
    skill_lifecycle_cli_call_count: int = 0
    manual_skill_bundle_accessed: bool = False
    manual_skill_bundle_names: tuple[str, ...] = ()
    manual_skill_bundle_access_count: int = 0
    tool_call_count: int = 0
    completion_gate_failure_count: int = 0
    completion_gate_incomplete_after_retries_count: int = 0
    forced_final_summary_count: int = 0
    verification_credit_miss_count: int = 0
    session_log_path: Path | None = None
    session_artifact_root: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class SkillsEvalAuthPreflightResult:
    ok: bool
    classification: SkillsEvalPreflightClassification
    message: str
    agent_exit_code: int | None
    session_log_path: Path | None = None
    session_artifact_root: Path | None = None


@dataclass(frozen=True)
class SkillsEvalVerificationResult:
    exit_code: int
    output_preview: str = ""


@dataclass(frozen=True)
class SkillsEvalRecord:
    case_id: str
    mode: str
    workspace: Path
    task: str
    invocation_mode: SkillsEvalInvocationMode
    explicit_skill_name: str | None
    expected_skills: tuple[str, ...]
    tags: tuple[str, ...]
    notes: str
    status: SkillsEvalRunStatus
    passed: bool | None
    skip_reason: str | None
    agent_exit_code: int | None
    verification_command: str | None
    verification_exit_code: int | None
    verification_output_preview: str | None
    skills_advertised_present: bool
    repo_conventions_present: bool
    matched_skill_context_attached: bool
    matched_skill_names: tuple[str, ...]
    explicit_skill_context_used: bool
    skill_read_called: bool
    skill_read_names: tuple[str, ...]
    skill_read_call_count: int
    manual_skill_bundle_accessed: bool
    manual_skill_bundle_names: tuple[str, ...]
    manual_skill_bundle_access_count: int
    tool_call_count: int
    session_log_path: Path | None
    session_artifact_root: Path | None
    skill_lifecycle_cli_used: bool = False
    skill_lifecycle_cli_commands: tuple[str, ...] = ()
    skill_lifecycle_cli_call_count: int = 0
    completion_gate_failure_count: int = 0
    completion_gate_incomplete_after_retries_count: int = 0
    forced_final_summary_count: int = 0
    verification_credit_miss_count: int = 0
    error: str | None = None

    def observed_skill_names(self) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for raw in (
            *self.matched_skill_names,
            *self.skill_read_names,
            *self.manual_skill_bundle_names,
            *(
                (self.explicit_skill_name,)
                if self.explicit_skill_context_used and self.explicit_skill_name
                else ()
            ),
        ):
            value = str(raw or "").strip()
            if not value:
                continue
            lowered = value.casefold()
            if lowered in seen:
                continue
            seen.add(lowered)
            names.append(value)
        return tuple(names)

    def relevant_skill_used(self) -> bool:
        expected = {item.casefold() for item in self.expected_skills}
        if not expected:
            return False
        return any(name.casefold() in expected for name in self.observed_skill_names())

    def any_skill_activity(self) -> bool:
        return bool(
            self.explicit_skill_context_used
            or self.matched_skill_context_attached
            or self.skill_read_called
            or self.manual_skill_bundle_accessed
        )

    def to_payload(self, *, output_dir: Path) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "mode": self.mode,
            "workspace": self.workspace.as_posix(),
            "task": self.task,
            "invocation_mode": self.invocation_mode,
            "explicit_skill_name": self.explicit_skill_name,
            "expected_skills": list(self.expected_skills),
            "tags": list(self.tags),
            "notes": self.notes,
            "status": self.status,
            "passed": self.passed,
            "skip_reason": self.skip_reason,
            "agent_exit_code": self.agent_exit_code,
            "verification_command": self.verification_command,
            "verification_exit_code": self.verification_exit_code,
            "verification_output_preview": self.verification_output_preview,
            "skills_advertised_present": self.skills_advertised_present,
            "repo_conventions_present": self.repo_conventions_present,
            "matched_skill_context_attached": self.matched_skill_context_attached,
            "matched_skill_names": list(self.matched_skill_names),
            "explicit_skill_context_used": self.explicit_skill_context_used,
            "skill_read_called": self.skill_read_called,
            "skill_read_names": list(self.skill_read_names),
            "skill_read_call_count": self.skill_read_call_count,
            "skill_lifecycle_cli_used": self.skill_lifecycle_cli_used,
            "skill_lifecycle_cli_commands": list(self.skill_lifecycle_cli_commands),
            "skill_lifecycle_cli_call_count": self.skill_lifecycle_cli_call_count,
            "manual_skill_bundle_accessed": self.manual_skill_bundle_accessed,
            "manual_skill_bundle_names": list(self.manual_skill_bundle_names),
            "manual_skill_bundle_access_count": self.manual_skill_bundle_access_count,
            "tool_call_count": self.tool_call_count,
            "completion_gate_failure_count": self.completion_gate_failure_count,
            "completion_gate_incomplete_after_retries_count": self.completion_gate_incomplete_after_retries_count,
            "forced_final_summary_count": self.forced_final_summary_count,
            "verification_credit_miss_count": self.verification_credit_miss_count,
            "session_log_path": _relativize_path(self.session_log_path, output_dir),
            "session_artifact_root": _relativize_path(self.session_artifact_root, output_dir),
            "observed_skill_names": list(self.observed_skill_names()),
            "relevant_skill_used": self.relevant_skill_used(),
            "error": self.error,
        }


@dataclass(frozen=True)
class SkillsEvalArtifacts:
    output_dir: Path
    results_path: Path
    summary_json_path: Path
    summary_md_path: Path
    records: tuple[SkillsEvalRecord, ...] = field(default_factory=tuple)
    summary: dict[str, object] = field(default_factory=dict)


def _relativize_path(path: Path | None, output_dir: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()
