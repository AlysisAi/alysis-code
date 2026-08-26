from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import workspace_isolation as _workspace_isolation
from .execution_shared import copy_workspace_snapshot, sync_snapshot_changed_files
from .forge import RunPaths
from .git_ops import GitOpsError, ensure_runtime_artifact_excludes
from .runtime_artifacts import is_runtime_artifact_path

_cleanup_workspace_path = _workspace_isolation._cleanup_workspace_path
_inspect_existing_git_workspace = _workspace_isolation._inspect_existing_git_workspace
_reset_git_workspace_to_target = _workspace_isolation._reset_git_workspace_to_target
_retry_readonly_removal = _workspace_isolation._retry_readonly_removal
_run_git_checked = _workspace_isolation._run_git_checked
_sanitize_existing_git_workspace = _workspace_isolation._sanitize_existing_git_workspace


@dataclass(frozen=True)
class SwarmBackendStartupResult:
    warnings: list[str]


@dataclass(frozen=True)
class PreparedTaskWorkspace:
    backend_name: str
    task_id: str
    branch: str
    base_branch: str
    worktree_path: Path
    control_root: Path


@dataclass(frozen=True)
class PreparedBatchCandidateWorkspace:
    backend_name: str
    batch_label: str
    branch: str
    base_branch: str
    worktree_path: Path
    control_root: Path


@dataclass(frozen=True)
class SwarmApplyResult:
    backend_name: str
    task_id: str
    branch: str
    merge_commit_hash: str
    action: str = "merged"


class SwarmBackend(Protocol):
    name: str
    capabilities: frozenset[str]
    requires_head_commit: bool
    supports_remote_sync: bool

    def prepare_startup(self, root: Path) -> SwarmBackendStartupResult: ...

    def prune(self, root: Path) -> None: ...

    def has_head_commit(self, root: Path) -> bool: ...

    def default_base_branch(self, root: Path) -> str: ...

    def branch_exists(self, root: Path, branch: str) -> bool: ...

    def task_workspace_path(self, *, run_dir: Path, task_id: str) -> Path: ...

    def candidate_workspace_path(self, *, run_dir: Path, batch_label: str) -> Path: ...

    def prepare_task_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        task_id: str,
        branch: str,
        base_branch: str,
    ) -> PreparedTaskWorkspace: ...

    def prepare_candidate_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        batch_label: str,
        branch: str,
        base_branch: str,
    ) -> PreparedBatchCandidateWorkspace: ...

    def load_task_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        task_id: str,
        branch: str,
        base_branch: str,
    ) -> PreparedTaskWorkspace: ...

    def apply_task_success(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        message: str,
        changed_files: list[str] | None = None,
    ) -> SwarmApplyResult: ...

    def apply_task_to_candidate(
        self,
        *,
        root: Path,
        candidate_workspace: PreparedBatchCandidateWorkspace,
        prepared_workspace: PreparedTaskWorkspace,
        message: str,
        changed_files: list[str] | None = None,
    ) -> None: ...

    def cleanup_task_workspace(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        keep_worktrees: bool,
    ) -> list[str]: ...

    def cleanup_candidate_workspace(
        self,
        *,
        root: Path,
        candidate_workspace: PreparedBatchCandidateWorkspace,
        keep_worktrees: bool,
    ) -> list[str]: ...

    def cleanup_failed_task_workspace(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        keep_worktrees: bool,
    ) -> list[str]: ...


def _require_materialized_candidate_workspace(
    *,
    worktree_path: Path,
    expected_branch: str,
) -> None:
    if not worktree_path.exists():
        raise GitOpsError(
            "candidate workspace was not materialized by ensure_worktree_fn; "
            f"expected a git worktree at {worktree_path} on branch {expected_branch}"
        )
    valid, _clean, reason = _inspect_existing_git_workspace(
        worktree_path=worktree_path,
        expected_branch=expected_branch,
    )
    if valid:
        return
    raise GitOpsError(
        "candidate workspace was not materialized correctly by ensure_worktree_fn: "
        f"{reason}. Expected a git worktree at {worktree_path} on branch {expected_branch}"
    )


def _tracked_runtime_artifact_paths(root: Path) -> list[str]:
    cp = _run_git_checked(
        root,
        ["ls-files", "-z"],
        error_message="failed to inspect tracked files",
    )
    return sorted(
        path
        for path in cp.stdout.split("\0")
        if path.strip() and is_runtime_artifact_path(path, root=root)
    )


def _failed_cleanup_marker_path(*, worktree_path: Path) -> Path:
    return worktree_path.parent / "failed_cleanup.json"


@dataclass(frozen=True)
class GitWorktreeSwarmBackend:
    ensure_worktree_fn: Any
    merge_runner: Any
    remove_worktree_fn: Any
    delete_branch_fn: Any
    prune_fn: Any
    branch_exists_fn: Any
    current_branch_fn: Any
    has_head_commit_fn: Any

    name: str = "git_worktree"
    capabilities: frozenset[str] = frozenset({"git", "merge", "worktree"})
    requires_head_commit: bool = True
    supports_remote_sync: bool = True

    def _clear_failed_cleanup_marker(self, *, worktree_path: Path) -> None:
        marker_path = _failed_cleanup_marker_path(worktree_path=worktree_path)
        if not marker_path.exists():
            return
        try:
            marker_path.unlink()
        except OSError as e:
            raise GitOpsError(f"failed to remove cleanup marker {marker_path}: {e}") from e

    def _write_failed_cleanup_marker(
        self,
        *,
        worktree_path: Path,
        branch: str,
        cleanup_errors: list[str],
        unresolved_state: list[str],
    ) -> None:
        marker_path = _failed_cleanup_marker_path(worktree_path=worktree_path)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "branch": branch,
            "cleanup_errors": cleanup_errors,
            "unresolved_state": unresolved_state,
        }
        try:
            marker_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            raise GitOpsError(f"failed to write cleanup marker {marker_path}: {e}") from e

    def _record_failed_cleanup_state(
        self,
        *,
        root: Path,
        worktree_path: Path,
        branch: str,
        cleanup_errors: list[str],
    ) -> list[str]:
        unresolved_state: list[str] = []
        if worktree_path.exists():
            unresolved_state.append(f"worktree path still exists: {worktree_path}")
        branch_name = str(branch or "").strip()
        if branch_name:
            try:
                if self.branch_exists(root, branch_name):
                    unresolved_state.append(f"task branch still exists: {branch_name}")
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"branch existence check failed: {e}")
        if cleanup_errors or unresolved_state:
            self._write_failed_cleanup_marker(
                worktree_path=worktree_path,
                branch=branch_name,
                cleanup_errors=list(cleanup_errors),
                unresolved_state=unresolved_state,
            )
            return unresolved_state
        self._clear_failed_cleanup_marker(worktree_path=worktree_path)
        return []

    def _cleanup_failed_branch(self, *, root: Path, branch: str) -> None:
        branch_name = str(branch or "").strip()
        if not branch_name or not self.branch_exists(root, branch_name):
            return
        try:
            self.delete_branch_fn(root, branch_name)
        except Exception:  # noqa: BLE001
            if not self.branch_exists(root, branch_name):
                return
            _run_git_checked(
                root,
                ["branch", "-D", branch_name],
                error_message=f"failed to force delete rejected task branch {branch_name}",
            )
            return
        if self.branch_exists(root, branch_name):
            _run_git_checked(
                root,
                ["branch", "-D", branch_name],
                error_message=f"failed to force delete rejected task branch {branch_name}",
            )

    def _retry_incomplete_failed_cleanup(
        self,
        *,
        root: Path,
        worktree_path: Path,
        branch: str,
    ) -> None:
        marker_path = _failed_cleanup_marker_path(worktree_path=worktree_path)
        if not marker_path.exists():
            return
        try:
            original_marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            original_marker_payload = None

        retry_errors: list[str] = []
        if worktree_path.exists():
            try:
                self.remove_worktree_fn(
                    root=root,
                    worktree_repo_path=worktree_path,
                    force=True,
                )
            except Exception as e:  # noqa: BLE001
                retry_errors.append(f"worktree cleanup failed: {e}")
        if branch:
            try:
                self._cleanup_failed_branch(root=root, branch=branch)
            except Exception as e:  # noqa: BLE001
                retry_errors.append(f"branch cleanup failed: {e}")

        unresolved_state = self._record_failed_cleanup_state(
            root=root,
            worktree_path=worktree_path,
            branch=branch,
            cleanup_errors=retry_errors,
        )
        if retry_errors or unresolved_state:
            marker_note = f" See {marker_path} for details."
            previous_note = ""
            if isinstance(original_marker_payload, dict):
                previous_errors = original_marker_payload.get("cleanup_errors")
                if isinstance(previous_errors, list) and previous_errors:
                    previous_note = f" Previous cleanup errors: {'; '.join(str(item) for item in previous_errors)}."
            unresolved_note = ""
            if unresolved_state:
                unresolved_note = f" Unresolved state: {'; '.join(unresolved_state)}."
            retry_note = ""
            if retry_errors:
                retry_note = f" Retry cleanup errors: {'; '.join(retry_errors)}."
            raise GitOpsError(
                "previous failed task cleanup left unresolved git-worktree state; rerun blocked "
                f"until cleanup succeeds.{previous_note}{retry_note}{unresolved_note}{marker_note}"
            )

    def prepare_startup(self, root: Path) -> SwarmBackendStartupResult:
        warnings: list[str] = []
        try:
            self.prune(root)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"git worktree prune failed: {e}")
        return SwarmBackendStartupResult(warnings=warnings)

    def prune(self, root: Path) -> None:
        self.prune_fn(root)

    def has_head_commit(self, root: Path) -> bool:
        return bool(self.has_head_commit_fn(root))

    def default_base_branch(self, root: Path) -> str:
        return str(self.current_branch_fn(root))

    def branch_exists(self, root: Path, branch: str) -> bool:
        return bool(self.branch_exists_fn(root, branch))

    def task_workspace_path(self, *, run_dir: Path, task_id: str) -> Path:
        return run_dir / "worktrees" / task_id / "repo"

    def candidate_workspace_path(self, *, run_dir: Path, batch_label: str) -> Path:
        return run_dir / "worktrees" / "_batch_candidates" / batch_label / "repo"

    def prepare_task_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        task_id: str,
        branch: str,
        base_branch: str,
    ) -> PreparedTaskWorkspace:
        worktree_path = self.task_workspace_path(run_dir=run_dir, task_id=task_id)
        self._retry_incomplete_failed_cleanup(
            root=root,
            worktree_path=worktree_path,
            branch=branch,
        )
        self.ensure_worktree_fn(
            root=root,
            worktree_repo_path=worktree_path,
            branch=branch,
            base_branch=base_branch,
        )
        return PreparedTaskWorkspace(
            backend_name=self.name,
            task_id=task_id,
            branch=branch,
            base_branch=base_branch,
            worktree_path=worktree_path,
            control_root=root,
        )

    def prepare_candidate_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        batch_label: str,
        branch: str,
        base_branch: str,
    ) -> PreparedBatchCandidateWorkspace:
        worktree_path = self.candidate_workspace_path(run_dir=run_dir, batch_label=batch_label)
        self._retry_incomplete_failed_cleanup(
            root=root,
            worktree_path=worktree_path,
            branch=branch,
        )
        self.ensure_worktree_fn(
            root=root,
            worktree_repo_path=worktree_path,
            branch=branch,
            base_branch=base_branch,
        )
        # Candidate verification runs real git operations inside the candidate repo.
        # Custom ensure_worktree_fn implementations must materialize the repo path
        # and check out the requested candidate branch before returning.
        _require_materialized_candidate_workspace(
            worktree_path=worktree_path,
            expected_branch=branch,
        )
        _reset_git_workspace_to_target(worktree_path=worktree_path, target=base_branch)
        return PreparedBatchCandidateWorkspace(
            backend_name=self.name,
            batch_label=batch_label,
            branch=branch,
            base_branch=base_branch,
            worktree_path=worktree_path,
            control_root=worktree_path,
        )

    def load_task_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        task_id: str,
        branch: str,
        base_branch: str,
    ) -> PreparedTaskWorkspace:
        return PreparedTaskWorkspace(
            backend_name=self.name,
            task_id=task_id,
            branch=branch,
            base_branch=base_branch,
            worktree_path=self.task_workspace_path(run_dir=run_dir, task_id=task_id),
            control_root=root,
        )

    def apply_task_success(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        message: str,
        changed_files: list[str] | None = None,
    ) -> SwarmApplyResult:
        _ = changed_files
        merge_commit_hash = self.merge_runner(
            root,
            base_branch=prepared_workspace.base_branch,
            task_branch=prepared_workspace.branch,
            message=message,
        )
        return SwarmApplyResult(
            backend_name=self.name,
            task_id=prepared_workspace.task_id,
            branch=prepared_workspace.branch,
            merge_commit_hash=merge_commit_hash,
            action="merged",
        )

    def apply_task_to_candidate(
        self,
        *,
        root: Path,
        candidate_workspace: PreparedBatchCandidateWorkspace,
        prepared_workspace: PreparedTaskWorkspace,
        message: str,
        changed_files: list[str] | None = None,
    ) -> None:
        _ = root
        _ = changed_files
        self.merge_runner(
            candidate_workspace.control_root,
            base_branch=candidate_workspace.branch,
            task_branch=prepared_workspace.branch,
            message=message,
        )

    def cleanup_task_workspace(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        keep_worktrees: bool,
    ) -> list[str]:
        if keep_worktrees:
            return []

        cleanup_errors: list[str] = []
        if prepared_workspace.worktree_path.exists():
            try:
                self.remove_worktree_fn(
                    root=root,
                    worktree_repo_path=prepared_workspace.worktree_path,
                    force=True,
                )
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"worktree cleanup failed: {e}")
        if prepared_workspace.branch:
            try:
                self.delete_branch_fn(root, prepared_workspace.branch)
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"branch cleanup failed: {e}")
        return cleanup_errors

    def cleanup_candidate_workspace(
        self,
        *,
        root: Path,
        candidate_workspace: PreparedBatchCandidateWorkspace,
        keep_worktrees: bool,
    ) -> list[str]:
        if keep_worktrees:
            return []

        cleanup_errors: list[str] = []
        if candidate_workspace.worktree_path.exists():
            try:
                self.remove_worktree_fn(
                    root=root,
                    worktree_repo_path=candidate_workspace.worktree_path,
                    force=True,
                )
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"candidate worktree cleanup failed: {e}")
        if candidate_workspace.branch:
            try:
                self.delete_branch_fn(root, candidate_workspace.branch)
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"candidate branch cleanup failed: {e}")
        try:
            unresolved_state = self._record_failed_cleanup_state(
                root=root,
                worktree_path=candidate_workspace.worktree_path,
                branch=candidate_workspace.branch,
                cleanup_errors=cleanup_errors,
            )
            if unresolved_state and not cleanup_errors:
                cleanup_errors.append(f"cleanup incomplete: {'; '.join(unresolved_state)}")
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"candidate cleanup marker failed: {e}")
        return cleanup_errors

    def cleanup_failed_task_workspace(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        keep_worktrees: bool,
    ) -> list[str]:
        if keep_worktrees:
            return []

        cleanup_errors: list[str] = []
        if prepared_workspace.worktree_path.exists():
            try:
                self.remove_worktree_fn(
                    root=root,
                    worktree_repo_path=prepared_workspace.worktree_path,
                    force=True,
                )
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"worktree cleanup failed: {e}")
        if prepared_workspace.branch:
            try:
                self._cleanup_failed_branch(root=root, branch=prepared_workspace.branch)
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"branch cleanup failed: {e}")
        try:
            unresolved_state = self._record_failed_cleanup_state(
                root=root,
                worktree_path=prepared_workspace.worktree_path,
                branch=prepared_workspace.branch,
                cleanup_errors=cleanup_errors,
            )
            if unresolved_state and not cleanup_errors:
                cleanup_errors.append(f"cleanup incomplete: {'; '.join(unresolved_state)}")
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"cleanup marker failed: {e}")
        return cleanup_errors


@dataclass(frozen=True)
class SnapshotSwarmBackend:
    merge_runner: Any
    branch_exists_fn: Any
    current_branch_fn: Any
    snapshot_base_branch: str = "snapshot-base"
    name: str = "snapshot_workspace"
    capabilities: frozenset[str] = frozenset({"git", "snapshot", "sync"})
    requires_head_commit: bool = False
    supports_remote_sync: bool = False

    def prepare_startup(self, root: Path) -> SwarmBackendStartupResult:
        _ = root
        return SwarmBackendStartupResult(warnings=[])

    def prune(self, root: Path) -> None:
        _ = root

    def has_head_commit(self, root: Path) -> bool:
        _ = root
        return False

    def default_base_branch(self, root: Path) -> str:
        try:
            branch = str(self.current_branch_fn(root) or "").strip()
        except Exception:  # noqa: BLE001
            branch = ""
        return branch or self.snapshot_base_branch

    def branch_exists(self, root: Path, branch: str) -> bool:
        return bool(self.branch_exists_fn(root, branch))

    def task_workspace_path(self, *, run_dir: Path, task_id: str) -> Path:
        return run_dir / "worktrees" / task_id / "repo"

    def candidate_workspace_path(self, *, run_dir: Path, batch_label: str) -> Path:
        return run_dir / "worktrees" / "_batch_candidates" / batch_label / "repo"

    def prepare_task_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        task_id: str,
        branch: str,
        base_branch: str,
    ) -> PreparedTaskWorkspace:
        worktree_path = self.task_workspace_path(run_dir=run_dir, task_id=task_id)
        if worktree_path.exists():
            valid, clean, _reason = _inspect_existing_git_workspace(
                worktree_path=worktree_path,
                expected_branch=branch,
            )
            if valid:
                if not clean:
                    _sanitize_existing_git_workspace(worktree_path=worktree_path)
                if not _tracked_runtime_artifact_paths(worktree_path):
                    return PreparedTaskWorkspace(
                        backend_name=self.name,
                        task_id=task_id,
                        branch=branch,
                        base_branch=base_branch,
                        worktree_path=worktree_path,
                        control_root=worktree_path,
                    )
            _cleanup_workspace_path(worktree_path)
        copy_workspace_snapshot(src_root=root, dest_root=worktree_path)
        self._init_snapshot_repo(
            worktree_path=worktree_path,
            base_branch=base_branch,
            task_branch=branch,
            task_id=task_id,
        )
        return PreparedTaskWorkspace(
            backend_name=self.name,
            task_id=task_id,
            branch=branch,
            base_branch=base_branch,
            worktree_path=worktree_path,
            control_root=worktree_path,
        )

    def prepare_candidate_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        batch_label: str,
        branch: str,
        base_branch: str,
    ) -> PreparedBatchCandidateWorkspace:
        worktree_path = self.candidate_workspace_path(run_dir=run_dir, batch_label=batch_label)
        if worktree_path.exists():
            _cleanup_workspace_path(worktree_path)
        copy_workspace_snapshot(src_root=root, dest_root=worktree_path)
        self._init_snapshot_repo(
            worktree_path=worktree_path,
            base_branch=base_branch,
            task_branch=branch,
            task_id=batch_label,
        )
        return PreparedBatchCandidateWorkspace(
            backend_name=self.name,
            batch_label=batch_label,
            branch=branch,
            base_branch=base_branch,
            worktree_path=worktree_path,
            control_root=worktree_path,
        )

    def load_task_workspace(
        self,
        *,
        root: Path,
        run_dir: Path,
        task_id: str,
        branch: str,
        base_branch: str,
    ) -> PreparedTaskWorkspace:
        _ = root
        worktree_path = self.task_workspace_path(run_dir=run_dir, task_id=task_id)
        if not worktree_path.exists():
            raise GitOpsError(
                "snapshot workspace is missing for ready_for_merge task; rerun the task first"
            )
        return PreparedTaskWorkspace(
            backend_name=self.name,
            task_id=task_id,
            branch=branch,
            base_branch=base_branch,
            worktree_path=worktree_path,
            control_root=worktree_path,
        )

    def apply_task_success(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        message: str,
        changed_files: list[str] | None = None,
    ) -> SwarmApplyResult:
        merge_commit_hash = self.merge_runner(
            prepared_workspace.control_root,
            base_branch=prepared_workspace.base_branch,
            task_branch=prepared_workspace.branch,
            message=message,
        )
        effective_changed_files = changed_files or self._changed_files_since_base(
            worktree_path=prepared_workspace.control_root,
            base_branch=prepared_workspace.base_branch,
        )
        sync_snapshot_changed_files(
            snapshot_root=prepared_workspace.worktree_path,
            workspace_root=root,
            changed_files=effective_changed_files,
        )
        return SwarmApplyResult(
            backend_name=self.name,
            task_id=prepared_workspace.task_id,
            branch=prepared_workspace.branch,
            merge_commit_hash=merge_commit_hash,
            action="applied",
        )

    def apply_task_to_candidate(
        self,
        *,
        root: Path,
        candidate_workspace: PreparedBatchCandidateWorkspace,
        prepared_workspace: PreparedTaskWorkspace,
        message: str,
        changed_files: list[str] | None = None,
    ) -> None:
        _ = root
        _ = message
        effective_changed_files = changed_files or self._changed_files_since_base(
            worktree_path=prepared_workspace.control_root,
            base_branch=prepared_workspace.base_branch,
        )
        sync_snapshot_changed_files(
            snapshot_root=prepared_workspace.worktree_path,
            workspace_root=candidate_workspace.worktree_path,
            changed_files=effective_changed_files,
        )

    def cleanup_task_workspace(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        keep_worktrees: bool,
    ) -> list[str]:
        _ = root
        if keep_worktrees:
            return []
        if not prepared_workspace.worktree_path.exists():
            return []
        try:
            _cleanup_workspace_path(prepared_workspace.worktree_path)
        except (GitOpsError, OSError) as e:
            return [f"snapshot cleanup failed: {e}"]
        return []

    def cleanup_candidate_workspace(
        self,
        *,
        root: Path,
        candidate_workspace: PreparedBatchCandidateWorkspace,
        keep_worktrees: bool,
    ) -> list[str]:
        _ = root
        if keep_worktrees:
            return []
        if not candidate_workspace.worktree_path.exists():
            return []
        try:
            _cleanup_workspace_path(candidate_workspace.worktree_path)
        except (GitOpsError, OSError) as e:
            return [f"snapshot candidate cleanup failed: {e}"]
        return []

    def cleanup_failed_task_workspace(
        self,
        *,
        root: Path,
        prepared_workspace: PreparedTaskWorkspace,
        keep_worktrees: bool,
    ) -> list[str]:
        _ = root
        if keep_worktrees:
            return []
        if not prepared_workspace.worktree_path.exists():
            return []
        try:
            _sanitize_existing_git_workspace(worktree_path=prepared_workspace.worktree_path)
        except Exception as e:  # noqa: BLE001
            try:
                _cleanup_workspace_path(prepared_workspace.worktree_path)
            except GitOpsError as cleanup_error:
                return [
                    "snapshot failure cleanup failed: "
                    f"{e}; fallback removal also failed: {cleanup_error}"
                ]
            return [f"snapshot failure cleanup reset failed; removed workspace instead: {e}"]
        return []

    def _init_snapshot_repo(
        self,
        *,
        worktree_path: Path,
        base_branch: str,
        task_branch: str,
        task_id: str,
    ) -> None:
        worktree_path.mkdir(parents=True, exist_ok=True)
        _run_git_checked(
            worktree_path, ["init", "-q"], error_message="failed to init snapshot repo"
        )
        _run_git_checked(
            worktree_path,
            ["config", "user.name", "alysis-agent"],
            error_message="failed to configure snapshot git user.name",
        )
        _run_git_checked(
            worktree_path,
            ["config", "user.email", "alysis-agent@local"],
            error_message="failed to configure snapshot git user.email",
        )
        ensure_runtime_artifact_excludes(worktree_path)
        _run_git_checked(
            worktree_path,
            ["checkout", "-b", base_branch],
            error_message=f"failed to create snapshot base branch {base_branch}",
        )
        _run_git_checked(
            worktree_path, ["add", "-A"], error_message="failed to stage snapshot files"
        )
        _run_git_checked(
            worktree_path,
            ["commit", "--allow-empty", "-m", f"Snapshot baseline for {task_id}"],
            error_message="failed to create snapshot baseline commit",
            extra_config={"user.name": "alysis-agent", "user.email": "alysis-agent@local"},
        )
        _run_git_checked(
            worktree_path,
            ["checkout", "-b", task_branch],
            error_message=f"failed to create snapshot task branch {task_branch}",
        )

    def _changed_files_since_base(self, *, worktree_path: Path, base_branch: str) -> list[str]:
        cp = _run_git_checked(
            worktree_path,
            ["diff", "--name-only", f"{base_branch}..HEAD"],
            error_message=f"failed to list snapshot changes from {base_branch}",
        )
        return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def select_swarm_backend(
    *,
    paths: RunPaths,
    ensure_worktree_fn: Any,
    merge_runner: Any,
    remove_worktree_fn: Any,
    delete_branch_fn: Any,
    prune_fn: Any,
    branch_exists_fn: Any,
    current_branch_fn: Any,
    has_head_commit_fn: Any,
) -> SwarmBackend:
    if paths.workspace_kind in {"plain_dir", "git_repo_no_head"} or not paths.has_head_commit:
        return SnapshotSwarmBackend(
            merge_runner=merge_runner,
            branch_exists_fn=branch_exists_fn,
            current_branch_fn=current_branch_fn,
        )
    return GitWorktreeSwarmBackend(
        ensure_worktree_fn=ensure_worktree_fn,
        merge_runner=merge_runner,
        remove_worktree_fn=remove_worktree_fn,
        delete_branch_fn=delete_branch_fn,
        prune_fn=prune_fn,
        branch_exists_fn=branch_exists_fn,
        current_branch_fn=current_branch_fn,
        has_head_commit_fn=has_head_commit_fn,
    )
