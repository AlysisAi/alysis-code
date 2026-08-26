from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .atomic_io import atomic_write_json, atomic_write_text
from .config import AppConfig
from .forge import RunPaths, ensure_execution_dirs, now_iso
from .knowledge_base import (
    KnowledgeEntry,
    KnowledgeIndexEntry,
    is_effectively_open_status,
    load_knowledge_index,
    rebuild_knowledge_index,
    write_issue_entry_for_task_id,
)
from .repo_scan import RepoScanResult
from .verify_gate import (
    CONFIG_VERIFY_COMMANDS_GENERIC_PRESET_SOURCE,
    ResolvedVerifyCommands,
    VerifyError,
    VerifyRunResult,
    is_generic_fallback_verify_command_selection,
    resolve_verify_command_selection,
    run_task_verification,
)

IntegrationVerifyMode = Literal["off", "warn", "strict"]


class IntegrationGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedIntegrationCommands:
    commands: tuple[str, ...]
    source: str

    def to_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "commands": list(self.commands),
        }


@dataclass(frozen=True)
class IntegrationGateResult:
    batch_index: int
    batch_label: str
    mode: IntegrationVerifyMode
    command_source: str
    commands: tuple[str, ...]
    merged_task_ids: tuple[str, ...]
    merged_paths: tuple[str, ...]
    verify_result: VerifyRunResult
    artifact_dir: Path
    result_path: Path
    commands_path: Path
    stdout_path: Path
    stderr_path: Path
    summary_path: Path
    verify_artifact_path: Path
    phase: str = "post_merge"
    verified_root: Path | None = None

    @property
    def passed(self) -> bool:
        return self.verify_result.all_passed

    @property
    def summary(self) -> str:
        return self.verify_result.summary

    @property
    def policy_outcome(self) -> str:
        if self.passed:
            return "passed"
        if self.mode == "warn":
            return "failed_tolerated_by_warn_policy"
        return "failed_blocking"

    def to_payload(self, *, root: Path) -> dict[str, object]:
        verified_root = self.verified_root or root
        return {
            "schema_version": 1,
            "generated_at": now_iso(),
            "batch_index": self.batch_index,
            "batch_label": self.batch_label,
            "mode": self.mode,
            "phase": self.phase,
            "passed": self.passed,
            "policy_outcome": self.policy_outcome,
            "tolerated_failure": self.policy_outcome == "failed_tolerated_by_warn_policy",
            "summary": self.summary,
            "failure_category": self.verify_result.failure_category_value,
            "command_source": self.command_source,
            "commands": list(self.commands),
            "merged_task_ids": list(self.merged_task_ids),
            "merged_paths": list(self.merged_paths),
            "verified_root": _display_path(root, verified_root),
            "verify_artifact_path": _repo_rel(root, self.verify_artifact_path),
            "commands_path": _repo_rel(root, self.commands_path),
            "stdout_path": _repo_rel(root, self.stdout_path),
            "stderr_path": _repo_rel(root, self.stderr_path),
            "summary_path": _repo_rel(root, self.summary_path),
            "result_path": _repo_rel(root, self.result_path),
            "command_results": [
                {
                    "command": item.command,
                    "effective_command": item.effective_command or item.command,
                    "exit_code": item.exit_code,
                    "ok": item.ok,
                    "real_execution": item.real_execution,
                    "non_execution_reason": item.non_execution_reason,
                    "fallback_used": item.fallback_used,
                    "fallback_reason": item.fallback_reason,
                }
                for item in self.verify_result.command_results
            ],
        }


def normalize_integration_verify_mode(mode: str) -> IntegrationVerifyMode:
    value = mode.strip().lower()
    if value not in {"off", "warn", "strict"}:
        raise IntegrationGateError(
            "Invalid integration verify mode. Use one of: off, warn, strict."
        )
    return value  # type: ignore[return-value]


def resolve_integration_verify_mode(
    *,
    cfg: AppConfig,
    integration_verify: str | None,
) -> IntegrationVerifyMode:
    raw = integration_verify if integration_verify is not None else cfg.integration_verify_mode
    return normalize_integration_verify_mode(raw)


def resolve_integration_verify_commands(
    *,
    cfg: AppConfig,
    integration_verify_cmd: list[str] | None,
    verify_cmd: list[str] | None = None,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
) -> ResolvedIntegrationCommands:
    def _resolve_verify_selection(scan: RepoScanResult | None) -> ResolvedVerifyCommands:
        try:
            return resolve_verify_command_selection(
                cfg=cfg,
                verify_cmd=verify_cmd,
                root=root,
                repo_scan=scan,
            )
        except VerifyError as e:
            if str(e) == "Configured verify_commands is empty.":
                raise IntegrationGateError(
                    "No integration verify commands are configured. Set integration_verify_commands or verify_commands."
                ) from e
            raise IntegrationGateError(str(e)) from e

    if integration_verify_cmd:
        commands = tuple(cmd.strip() for cmd in integration_verify_cmd if cmd.strip())
        if not commands:
            raise IntegrationGateError("--integration-verify-cmd values cannot be empty.")
        return ResolvedIntegrationCommands(
            commands=commands,
            source="cli.integration_verify_cmd",
        )

    configured_integration = tuple(
        cmd.strip() for cmd in cfg.integration_verify_commands if cmd.strip()
    )
    if configured_integration:
        return ResolvedIntegrationCommands(
            commands=configured_integration,
            source="config.integration_verify_commands",
        )

    verify_resolution = _resolve_verify_selection(repo_scan)

    raw_source = verify_resolution.source
    source = {
        "cli.verify_cmd": "cli.verify_cmd_fallback",
        "config.verify_commands": "config.verify_commands_fallback",
        "repo_scan.likely_test_commands": "repo_scan.likely_test_commands_fallback",
        "config.verify_commands_fallback": "config.verify_commands_fallback",
        CONFIG_VERIFY_COMMANDS_GENERIC_PRESET_SOURCE: (
            "config.verify_commands_generic_preset_fallback"
        ),
    }.get(raw_source, raw_source)
    if is_generic_fallback_verify_command_selection(verify_resolution) and (
        root is not None or repo_scan is not None
    ):
        return ResolvedIntegrationCommands(
            commands=(),
            source="repo_scan.no_authoritative_commands_fallback",
        )
    return ResolvedIntegrationCommands(
        commands=verify_resolution.commands,
        source=source,
    )


def run_integration_gate(
    *,
    paths: RunPaths,
    cfg: AppConfig,
    batch_index: int,
    mode: IntegrationVerifyMode,
    merged_task_ids: list[str],
    merged_paths: list[str],
    integration_verify_cmd: list[str] | None = None,
    verify_cmd: list[str] | None = None,
    root: Path | None = None,
    repo_scan: RepoScanResult | None = None,
    phase: str = "post_merge",
) -> IntegrationGateResult:
    ensure_execution_dirs(paths)
    resolved_root = (root or paths.root).resolve()
    batch_label = f"batch_{batch_index:03d}"
    artifact_dir = paths.execution_integration_dir / batch_label
    artifact_dir.mkdir(parents=True, exist_ok=True)
    command_resolution = resolve_integration_verify_commands(
        cfg=cfg,
        integration_verify_cmd=integration_verify_cmd,
        verify_cmd=verify_cmd,
        root=resolved_root,
        repo_scan=repo_scan,
    )
    verify_artifact_path = artifact_dir / "verify.txt"
    verify_result = run_task_verification(
        root=resolved_root,
        commands=list(command_resolution.commands),
        artifact_path=verify_artifact_path,
        cfg=cfg,
    )
    commands_path = artifact_dir / "commands.json"
    stdout_path = artifact_dir / "stdout.txt"
    stderr_path = artifact_dir / "stderr.txt"
    summary_path = artifact_dir / "summary.md"
    result_path = artifact_dir / "result.json"
    atomic_write_json(
        commands_path,
        {
            "schema_version": 1,
            "generated_at": now_iso(),
            "batch_index": batch_index,
            "batch_label": batch_label,
            "mode": mode,
            "phase": phase,
            "command_source": command_resolution.source,
            "commands": list(command_resolution.commands),
            "merged_task_ids": list(merged_task_ids),
            "verified_root": _display_path(paths.root, resolved_root),
        },
    )
    atomic_write_text(stdout_path, _render_command_stream(verify_result, stream_name="stdout"))
    atomic_write_text(stderr_path, _render_command_stream(verify_result, stream_name="stderr"))

    result = IntegrationGateResult(
        batch_index=batch_index,
        batch_label=batch_label,
        mode=mode,
        command_source=command_resolution.source,
        commands=command_resolution.commands,
        merged_task_ids=tuple(merged_task_ids),
        merged_paths=tuple(_dedupe_preserve_order(merged_paths)),
        verify_result=verify_result,
        artifact_dir=artifact_dir,
        result_path=result_path,
        commands_path=commands_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        summary_path=summary_path,
        verify_artifact_path=verify_artifact_path,
        phase=phase,
        verified_root=resolved_root,
    )
    atomic_write_text(summary_path, _render_integration_summary(result=result, root=paths.root))
    atomic_write_json(result_path, result.to_payload(root=paths.root))
    return result


def integration_issue_signature_for_commands(commands: tuple[str, ...] | list[str]) -> str:
    normalized_commands = [
        " ".join(str(command).strip().split()) for command in commands if str(command).strip()
    ]
    payload = json.dumps(normalized_commands, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"integration_gate_v1:{digest}"


def write_integration_failure_issue(
    *,
    paths: RunPaths,
    result: IntegrationGateResult,
) -> KnowledgeEntry:
    title = f"{result.batch_label}: integration verification failed"
    summary = result.summary
    issue = write_issue_entry_for_task_id(
        paths=paths,
        task_id=result.batch_label,
        source="integration_gate",
        title=title,
        summary=summary,
        paths_in_scope=list(result.merged_paths),
        report_path=result.summary_path,
        verify_artifact_path=result.verify_artifact_path,
        related_tasks=list(result.merged_task_ids),
        tags=["integration_gate", "integration_failure", result.mode],
        status="open",
        signature=integration_issue_signature_for_commands(result.commands),
    )
    return issue


def _matching_open_integration_issues(
    *,
    paths: RunPaths,
    signature: str,
) -> tuple[KnowledgeIndexEntry, ...]:
    index = load_knowledge_index(paths, rebuild=True)
    matches = [
        entry
        for entry in index.entries
        if entry.kind == "issue"
        and entry.source == "integration_gate"
        and entry.signature == signature
        and is_effectively_open_status(entry.effective_status or entry.status)
    ]
    return tuple(matches)


def write_integration_resolution_issue(
    *,
    paths: RunPaths,
    result: IntegrationGateResult,
    resolved_issue_ids: tuple[str, ...],
    signature: str,
) -> KnowledgeEntry:
    return write_issue_entry_for_task_id(
        paths=paths,
        task_id=result.batch_label,
        source="integration_gate",
        title=f"{result.batch_label}: integration verification passed",
        summary=result.summary,
        paths_in_scope=list(result.merged_paths),
        report_path=result.summary_path,
        verify_artifact_path=result.verify_artifact_path,
        related_tasks=list(result.merged_task_ids),
        tags=["integration_gate", "integration_resolution", result.mode],
        status="resolved",
        signature=signature,
        resolves=list(resolved_issue_ids),
    )


def write_integration_issue_summary(paths: RunPaths) -> Path:
    ensure_execution_dirs(paths)
    index = load_knowledge_index(paths, rebuild=True)
    open_issues = [
        entry
        for entry in index.entries
        if entry.kind == "issue"
        and entry.source == "integration_gate"
        and is_effectively_open_status(entry.effective_status or entry.status)
    ]
    lines = [
        "# Integration Issues",
        "",
        f"- Generated At: `{now_iso()}`",
        "",
    ]
    if not open_issues:
        lines.append("- No open integration issues.")
    else:
        for entry in open_issues:
            line = f"- `{entry.id}` {entry.title}"
            if entry.related_tasks:
                line += f" related tasks: {', '.join(entry.related_tasks)}"
            if entry.paths:
                line += f" paths: {', '.join(entry.paths[:5])}"
            line += f" file: `{entry.knowledge_file_path}`"
            lines.append(line)
    paths.execution_integration_issues_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.execution_integration_issues_path, "\n".join(lines).rstrip() + "\n")
    return paths.execution_integration_issues_path


def record_integration_failure_knowledge(
    *,
    paths: RunPaths,
    result: IntegrationGateResult,
) -> tuple[KnowledgeEntry, Path]:
    issue = write_integration_failure_issue(paths=paths, result=result)
    rebuild_knowledge_index(paths)
    summary_path = write_integration_issue_summary(paths)
    return issue, summary_path


def record_integration_resolution_knowledge(
    *,
    paths: RunPaths,
    result: IntegrationGateResult,
) -> tuple[KnowledgeEntry | None, Path]:
    signature = integration_issue_signature_for_commands(result.commands)
    open_issues = _matching_open_integration_issues(paths=paths, signature=signature)
    resolution_entry: KnowledgeEntry | None = None
    if open_issues:
        resolution_entry = write_integration_resolution_issue(
            paths=paths,
            result=result,
            resolved_issue_ids=tuple(entry.id for entry in open_issues),
            signature=signature,
        )
    rebuild_knowledge_index(paths)
    summary_path = write_integration_issue_summary(paths)
    return resolution_entry, summary_path


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _repo_rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _display_path(root: Path, path: Path) -> str:
    try:
        return _repo_rel(root, path)
    except ValueError:
        return path.resolve().as_posix()


def _render_command_stream(result: VerifyRunResult, *, stream_name: str) -> str:
    lines = [f"# Integration {stream_name.title()}"]
    for index, item in enumerate(result.command_results, start=1):
        text = item.stdout if stream_name == "stdout" else item.stderr
        lines.extend(
            [
                "",
                f"## Command {index}",
                f"requested_command: {item.command}",
                f"effective_command: {item.effective_command or item.command}",
                text.rstrip() or "(no output)",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_integration_summary(*, result: IntegrationGateResult, root: Path) -> str:
    verified_root = result.verified_root or root
    if result.phase == "pre_merge_candidate":
        phase_label = "pre-merge candidate"
        task_heading = "Batch Tasks"
    elif result.phase == "final_repo":
        phase_label = "final repo"
        task_heading = "Final Repo Tasks"
    else:
        phase_label = "post-merge base"
        task_heading = "Merged Tasks"
    lines = [
        "# Integration Gate Summary",
        "",
        f"- Batch: `{result.batch_label}`",
        f"- Phase: `{phase_label}`",
        f"- Mode: `{result.mode}`",
        f"- Passed: `{'yes' if result.passed else 'no'}`",
        f"- Policy Outcome: `{result.policy_outcome}`",
        f"- Summary: {result.summary}",
        f"- Command Source: `{result.command_source}`",
        f"- Verified Root: `{_display_path(root, verified_root)}`",
        f"- Commands Artifact: `{_repo_rel(root, result.commands_path)}`",
        f"- Verify Artifact: `{_repo_rel(root, result.verify_artifact_path)}`",
        f"- Stdout Artifact: `{_repo_rel(root, result.stdout_path)}`",
        f"- Stderr Artifact: `{_repo_rel(root, result.stderr_path)}`",
        "",
        f"## {task_heading}",
        "",
    ]
    if result.merged_task_ids:
        lines.extend(f"- `{task_id}`" for task_id in result.merged_task_ids)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Commands", ""])
    if result.commands:
        lines.extend(f"- `{cmd}`" for cmd in result.commands)
    else:
        lines.append("- (none)")
    return "\n".join(lines).rstrip() + "\n"
