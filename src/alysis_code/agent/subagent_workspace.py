from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from ..atomic_io import atomic_write_text
from ..git_evidence import CandidateGitState, capture_candidate_git_state
from ..session_store import SessionStore
from ..workspace_isolation import GitOpsError, _cleanup_workspace_path, _run_git_checked

WorkspaceState = Literal["prepared", "captured", "applied", "discarded"]
WorkspaceReleaseAction = Literal["applied", "discarded"]

_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PATCH_FAILED_PATH_RE = re.compile(r"patch failed: (.+?):\d+(?:\s|$)")
_PATCH_DOES_NOT_APPLY_PATH_RE = re.compile(r"error: (.+?): patch does not apply")


@dataclass
class SubagentWorkspaceRecord:
    run_id: str
    worktree_path: Path
    base_commit: str
    parent_dirty_paths: tuple[str, ...]
    state: WorkspaceState = "prepared"
    evidence: CandidateGitState | None = None
    patch_artifact: str = ""
    pinned_by_run_ids: tuple[str, ...] = ()
    no_changes: bool = False
    cleanup_pending: bool = False
    cleanup_error: str = ""


class SubagentWorkspaceProvider:
    """Own isolated Git worktrees for one parent agent session."""

    def __init__(self, *, root: Path, store: SessionStore) -> None:
        self.root = root.resolve()
        self.store = store
        self.worktrees_root = store.session_artifact_root / "subagent_worktrees"
        self._records: dict[str, SubagentWorkspaceRecord] = {}
        self._lock = RLock()

    def _event(self, run_id: str, action: str, **fields: Any) -> None:
        self.store.append(
            "subagent_workspace",
            {"run_id": run_id, "action": action, **fields},
        )

    @staticmethod
    def _error(*, run_id: str, error_code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "run_id": run_id,
            "error": message,
            "error_code": error_code,
        }

    @classmethod
    def _release_locked_error(cls, record: SubagentWorkspaceRecord) -> dict[str, Any]:
        return {
            **cls._error(
                run_id=record.run_id,
                error_code="workspace_release_locked",
                message=(
                    f"Isolated workspace {record.run_id} is pinned by a running child: "
                    f"{', '.join(record.pinned_by_run_ids)}."
                ),
            ),
            "pinned_by_run_ids": list(record.pinned_by_run_ids),
        }

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        normalized = str(run_id or "").strip()
        if not _SAFE_RUN_ID_RE.fullmatch(normalized):
            raise ValueError("invalid subagent workspace run id")
        return normalized

    def _parent_git_context(self) -> tuple[str, tuple[str, ...]]:
        inside = _run_git_checked(
            self.root,
            ["rev-parse", "--is-inside-work-tree"],
            error_message="workspace_view=isolated requires a git repository",
        )
        if inside.stdout.strip().lower() != "true":
            raise GitOpsError("workspace_view=isolated requires a git repository")
        base_commit = _run_git_checked(
            self.root,
            ["rev-parse", "HEAD"],
            error_message="failed to resolve parent HEAD",
        ).stdout.strip()
        if not base_commit:
            raise GitOpsError("failed to resolve parent HEAD")
        status = _run_git_checked(
            self.root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            error_message="failed to inspect parent workspace",
        ).stdout
        return base_commit, _porcelain_paths(status)

    def prepare(self, run_id: str) -> dict[str, Any]:
        try:
            normalized_run_id = self._validate_run_id(run_id)
        except ValueError as exc:
            return self._error(
                run_id=str(run_id or ""),
                error_code="invalid_workspace_run_id",
                message=str(exc),
            )
        with self._lock:
            if normalized_run_id in self._records:
                return self._error(
                    run_id=normalized_run_id,
                    error_code="workspace_run_already_exists",
                    message=f"An isolated workspace already exists for run {normalized_run_id}.",
                )
            try:
                base_commit, parent_dirty_paths = self._parent_git_context()
            except GitOpsError:
                return self._error(
                    run_id=normalized_run_id,
                    error_code="isolated_workspace_requires_git",
                    message="workspace_view=isolated requires a git repository",
                )

            worktree_path = self.worktrees_root / normalized_run_id
            if worktree_path.exists():
                return self._error(
                    run_id=normalized_run_id,
                    error_code="workspace_path_exists",
                    message=f"Isolated workspace path already exists for run {normalized_run_id}.",
                )
            self.worktrees_root.mkdir(parents=True, exist_ok=True)
            try:
                _run_git_checked(
                    self.root,
                    ["worktree", "add", "--detach", os.fspath(worktree_path), base_commit],
                    error_message="failed to create isolated subagent workspace",
                )
            except GitOpsError as exc:
                return self._error(
                    run_id=normalized_run_id,
                    error_code="workspace_prepare_failed",
                    message=str(exc),
                )

            record = SubagentWorkspaceRecord(
                run_id=normalized_run_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                parent_dirty_paths=parent_dirty_paths,
            )
            self._records[normalized_run_id] = record
            self._event(
                normalized_run_id,
                "prepared",
                base_commit=base_commit,
                parent_dirty_paths=list(parent_dirty_paths),
            )
            return {
                "ok": True,
                "run_id": normalized_run_id,
                "worktree_path": os.fspath(worktree_path),
                "base_commit": base_commit,
                "parent_dirty_paths": list(parent_dirty_paths),
            }

    def capture(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return self._error(
                    run_id=str(run_id or ""),
                    error_code="unknown_workspace_run",
                    message=f"Unknown isolated workspace run: {run_id}",
                )
            if record.state in {"applied", "discarded"}:
                return self._error(
                    run_id=record.run_id,
                    error_code="workspace_already_released",
                    message=f"Isolated workspace {record.run_id} was already {record.state}.",
                )
            evidence = capture_candidate_git_state(
                worktree_path=record.worktree_path,
                base_ref=record.base_commit,
            )
            artifact_path = self.store.session_artifact_layout.artifact_fs_path(
                "subagent_patches",
                f"{record.run_id}.patch",
            )
            atomic_write_text(artifact_path, evidence.patch_text)
            patch_artifact = self.store.session_artifact_layout.locator_for_path(artifact_path)
            record.evidence = evidence
            record.patch_artifact = patch_artifact
            record.state = "captured"
            paths = list(evidence.material_changed_paths)
            no_changes = not paths
            record.no_changes = no_changes
            self._event(
                record.run_id,
                "captured",
                base_commit=record.base_commit,
                patch_artifact=patch_artifact,
                paths=paths,
                no_changes=no_changes,
            )
            result = {
                "ok": True,
                "run_id": record.run_id,
                "base_commit": record.base_commit,
                "parent_dirty_paths": list(record.parent_dirty_paths),
                "patch_artifact": patch_artifact,
                "paths": paths,
                "insertions": _patch_line_count(evidence.patch_text, prefix="+"),
                "deletions": _patch_line_count(evidence.patch_text, prefix="-"),
                "sha256": evidence.patch_text_sha256,
                "patch_capture_status": evidence.patch_capture_status,
                "material_patch_complete": evidence.material_patch_complete,
                "no_changes": no_changes,
            }
            if no_changes:
                released = self.release(
                    record.run_id,
                    action="discarded",
                    reason="no_changes",
                )
                if not bool(released.get("ok")):
                    return released
            return result

    def get(self, run_id: str) -> SubagentWorkspaceRecord | None:
        with self._lock:
            record = self._records.get(str(run_id or "").strip())
            return replace(record) if record is not None else None

    def reattach_for_resume(self, source_run_id: str, new_run_id: str) -> dict[str, Any]:
        try:
            source = self._validate_run_id(source_run_id)
            target = self._validate_run_id(new_run_id)
        except ValueError as exc:
            return self._error(
                run_id=str(new_run_id or ""),
                error_code="invalid_workspace_run_id",
                message=str(exc),
            )
        with self._lock:
            record = self._records.get(source)
            if record is None:
                return self._error(
                    run_id=source,
                    error_code="unknown_workspace_run",
                    message=f"Unknown isolated workspace run: {source}",
                )
            if record.state in {"applied", "discarded"}:
                return self._error(
                    run_id=source,
                    error_code="subagent_resume_worktree_released",
                    message=f"Isolated workspace {source} was already {record.state}.",
                )
            if record.pinned_by_run_ids:
                return self._release_locked_error(record)
            if target in self._records:
                return self._error(
                    run_id=target,
                    error_code="workspace_run_already_exists",
                    message=f"An isolated workspace already exists for run {target}.",
                )
            self._records.pop(source)
            record.run_id = target
            self._records[target] = record
            self._event(
                target,
                "reattached",
                resumed_from=source,
                base_commit=record.base_commit,
                patch_artifact=record.patch_artifact or None,
            )
            return {
                "ok": True,
                "run_id": target,
                "resumed_from": source,
                "worktree_path": os.fspath(record.worktree_path),
                "patch_artifact": record.patch_artifact or None,
            }

    def inspect_pin_source(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self._inspect_pin_source_locked(str(run_id or "").strip())

    def _inspect_pin_source_locked(self, run_id: str) -> dict[str, Any]:
        record = self._records.get(run_id)
        if record is None:
            return self._error(
                run_id=run_id,
                error_code="unknown_workspace_from_run",
                message=f"Unknown isolated workspace run: {run_id}",
            )
        if record.state in {"applied", "discarded"}:
            return self._error(
                run_id=record.run_id,
                error_code="workspace_from_run_released",
                message=f"Isolated workspace {record.run_id} was already {record.state}.",
            )
        if record.state != "captured" or record.evidence is None:
            return self._error(
                run_id=record.run_id,
                error_code="workspace_from_run_not_ready",
                message=f"Isolated workspace {record.run_id} is not a completed result.",
            )
        return {
            "ok": True,
            "run_id": record.run_id,
            "worktree_path": os.fspath(record.worktree_path),
            "base_commit": record.base_commit,
            "paths": list(record.evidence.material_changed_paths),
        }

    def acquire_pin(self, run_id: str, *, consumer_run_id: str) -> dict[str, Any]:
        with self._lock:
            inspected = self._inspect_pin_source_locked(str(run_id or "").strip())
            if not bool(inspected.get("ok")):
                return inspected
            record = self._records[str(inspected["run_id"])]
            consumer = str(consumer_run_id or "").strip()
            if consumer not in record.pinned_by_run_ids:
                record.pinned_by_run_ids = (*record.pinned_by_run_ids, consumer)
                self._event(
                    record.run_id,
                    "pinned",
                    consumer_run_id=consumer,
                    pinned_by_run_ids=list(record.pinned_by_run_ids),
                )
            return inspected

    def release_pin(self, run_id: str, *, consumer_run_id: str) -> None:
        with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return
            consumer = str(consumer_run_id or "").strip()
            if consumer not in record.pinned_by_run_ids:
                return
            record.pinned_by_run_ids = tuple(
                candidate for candidate in record.pinned_by_run_ids if candidate != consumer
            )
            self._event(
                record.run_id,
                "unpinned",
                consumer_run_id=consumer,
                pinned_by_run_ids=list(record.pinned_by_run_ids),
            )

    def unapplied_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [
                replace(record)
                for record in self._records.values()
                if record.state == "captured" and not record.no_changes
            ]
        summaries: list[dict[str, Any]] = []
        for record in records:
            evidence = record.evidence
            patch_text = evidence.patch_text if evidence is not None else ""
            summaries.append(
                {
                    "run_id": record.run_id,
                    "files": (
                        list(evidence.material_changed_paths) if evidence is not None else []
                    ),
                    "insertions": _patch_line_count(patch_text, prefix="+"),
                    "deletions": _patch_line_count(patch_text, prefix="-"),
                }
            )
        return summaries

    def apply(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return self._error(
                    run_id=str(run_id or ""),
                    error_code="unknown_workspace_run",
                    message=f"Unknown isolated workspace run: {run_id}",
                )
            if record.no_changes:
                return self._error(
                    run_id=record.run_id,
                    error_code="no_changes",
                    message=f"Isolated workspace {record.run_id} produced no changes.",
                )
            if record.state in {"applied", "discarded"}:
                return self._error(
                    run_id=record.run_id,
                    error_code=f"workspace_already_{record.state}",
                    message=f"Isolated workspace {record.run_id} was already {record.state}.",
                )
            if record.pinned_by_run_ids:
                return self._release_locked_error(record)
            if record.evidence is None or record.state != "captured":
                return self._error(
                    run_id=record.run_id,
                    error_code="workspace_not_captured",
                    message=f"Isolated workspace {record.run_id} has not been captured.",
                )
            evidence = record.evidence
            paths = list(evidence.material_changed_paths)
            if not paths or not evidence.patch_text:
                return self._error(
                    run_id=record.run_id,
                    error_code="no_captured_changes",
                    message=f"Isolated workspace {record.run_id} has no captured changes.",
                )
            if not evidence.material_patch_complete:
                return self._error(
                    run_id=record.run_id,
                    error_code="incomplete_workspace_patch",
                    message=f"Isolated workspace {record.run_id} has an incomplete patch.",
                )

            patch_path = self.store.session_artifact_layout.artifact_fs_path(
                "subagent_patches",
                f"{record.run_id}.patch",
            )
            try:
                _run_git_checked(
                    self.root,
                    ["apply", "--check", os.fspath(patch_path)],
                    error_message="isolated subagent patch conflicts with the parent workspace",
                )
            except GitOpsError as exc:
                conflicts = _conflicting_patch_paths(str(exc), fallback=paths)
                self._event(
                    record.run_id,
                    "conflict",
                    base_commit=record.base_commit,
                    patch_artifact=record.patch_artifact,
                    paths=conflicts,
                )
                return {
                    "ok": False,
                    "run_id": record.run_id,
                    "error": str(exc),
                    "error_code": "merge_conflict",
                    "conflicting_paths": conflicts,
                    "patch_artifact": record.patch_artifact,
                }

            try:
                _run_git_checked(
                    self.root,
                    ["apply", os.fspath(patch_path)],
                    error_message="failed to apply isolated subagent patch",
                )
            except GitOpsError as exc:
                conflicts = _conflicting_patch_paths(str(exc), fallback=paths)
                self._event(
                    record.run_id,
                    "conflict",
                    base_commit=record.base_commit,
                    patch_artifact=record.patch_artifact,
                    paths=conflicts,
                )
                return {
                    "ok": False,
                    "run_id": record.run_id,
                    "error": str(exc),
                    "error_code": "merge_conflict",
                    "conflicting_paths": conflicts,
                    "patch_artifact": record.patch_artifact,
                }

            summary = {
                "files": paths,
                "insertions": _patch_line_count(evidence.patch_text, prefix="+"),
                "deletions": _patch_line_count(evidence.patch_text, prefix="-"),
                "patch_artifact": record.patch_artifact,
                "sha256": evidence.patch_text_sha256,
            }
            # The parent mutation is authoritative once ``git apply`` succeeds.
            # Worktree cleanup is ancillary and cannot roll that mutation back,
            # so record the logical state before attempting cleanup.
            record.state = "applied"
            self._event(
                record.run_id,
                "applied",
                base_commit=record.base_commit,
                patch_artifact=record.patch_artifact or None,
                paths=paths,
            )
            result: dict[str, Any] = {
                "ok": True,
                "run_id": record.run_id,
                "applied_paths": paths,
                "patch_summary": summary,
            }
            cleanup_error = self._remove_worktree(record.worktree_path)
            if cleanup_error:
                record.cleanup_pending = True
                record.cleanup_error = cleanup_error
                self._event(
                    record.run_id,
                    "cleanup_failed",
                    release_action="applied",
                    error=cleanup_error,
                )
                result.update(
                    {
                        "cleanup_pending": True,
                        "cleanup_warning": cleanup_error,
                    }
                )
            return result

    def release(
        self,
        run_id: str,
        *,
        action: WorkspaceReleaseAction,
        reason: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return self._error(
                    run_id=str(run_id or ""),
                    error_code="unknown_workspace_run",
                    message=f"Unknown isolated workspace run: {run_id}",
                )
            if record.state in {"applied", "discarded"}:
                return self._error(
                    run_id=record.run_id,
                    error_code=f"workspace_already_{record.state}",
                    message=f"Isolated workspace {record.run_id} was already {record.state}.",
                )
            if record.pinned_by_run_ids:
                return self._release_locked_error(record)
            cleanup_error = self._remove_worktree(record.worktree_path)
            if cleanup_error:
                return self._error(
                    run_id=record.run_id,
                    error_code="workspace_release_failed",
                    message=cleanup_error,
                )
            record.state = action
            paths = (
                list(record.evidence.material_changed_paths) if record.evidence is not None else []
            )
            self._event(
                record.run_id,
                action,
                base_commit=record.base_commit,
                patch_artifact=record.patch_artifact or None,
                paths=paths,
                **({"reason": reason} if reason else {}),
            )
            return {
                "ok": True,
                "run_id": record.run_id,
                "action": action,
                "paths": paths,
                "patch_artifact": record.patch_artifact or None,
            }

    def _remove_worktree(self, worktree_path: Path) -> str:
        try:
            _run_git_checked(
                self.root,
                ["worktree", "remove", "--force", os.fspath(worktree_path)],
                error_message="failed to remove isolated subagent workspace",
            )
            return ""
        except GitOpsError as exc:
            try:
                _cleanup_workspace_path(worktree_path)
                _run_git_checked(
                    self.root,
                    ["worktree", "prune"],
                    error_message="failed to prune isolated subagent workspace metadata",
                )
            except GitOpsError:
                return str(exc)
            return ""

    def close(self) -> None:
        with self._lock:
            for record in self._records.values():
                record.pinned_by_run_ids = ()
            pending = [
                run_id
                for run_id, record in self._records.items()
                if record.state not in {"applied", "discarded"}
            ]
            cleanup_pending = [
                run_id for run_id, record in self._records.items() if record.cleanup_pending
            ]
        for run_id in pending:
            self.release(run_id, action="discarded")
        for run_id in cleanup_pending:
            with self._lock:
                record = self._records.get(run_id)
                if record is None or not record.cleanup_pending:
                    continue
                cleanup_error = self._remove_worktree(record.worktree_path)
                if cleanup_error:
                    record.cleanup_error = cleanup_error
                    self._event(
                        record.run_id,
                        "cleanup_failed",
                        release_action=record.state,
                        error=cleanup_error,
                        retry="session_close",
                    )
                    continue
                record.cleanup_pending = False
                record.cleanup_error = ""
                self._event(
                    record.run_id,
                    "cleanup_completed",
                    release_action=record.state,
                    retry="session_close",
                )


def _porcelain_paths(status: str) -> tuple[str, ...]:
    records = status.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        state = record[:2]
        path = record[3:] if len(record) > 3 else ""
        if path:
            paths.add(path.replace("\\", "/"))
        if any(marker in state for marker in ("R", "C")) and index < len(records):
            original = records[index]
            index += 1
            if original:
                paths.add(original.replace("\\", "/"))
    return tuple(sorted(paths))


def _patch_line_count(patch_text: str, *, prefix: str) -> int:
    header = f"{prefix}{prefix}{prefix}"
    return sum(
        1
        for line in patch_text.splitlines()
        if line.startswith(prefix) and not line.startswith(header)
    )


def _conflicting_patch_paths(message: str, *, fallback: list[str]) -> list[str]:
    found = {
        match.group(1).strip().replace("\\", "/")
        for pattern in (_PATCH_FAILED_PATH_RE, _PATCH_DOES_NOT_APPLY_PATH_RE)
        for match in pattern.finditer(message)
        if match.group(1).strip()
    }
    return sorted(found) if found else sorted(set(fallback))
