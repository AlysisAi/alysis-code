from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from ..atomic_io import atomic_write_text
from ..git_evidence import (
    CandidateGitState,
    WorkspaceSnapshotError,
    capture_isolated_workspace_git_state,
    snapshot_workspace_baseline,
)
from ..session_store import SessionStore
from ..workspace_isolation import GitOpsError, _cleanup_workspace_path, _run_git_checked

WorkspaceState = Literal["prepared", "captured", "applied", "discarded", "duplicate"]
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
    material_identity: SubagentMaterialIdentity | None = None
    duplicate_of: str = ""
    already_integrated: bool = False
    parent_head_commit: str = ""


@dataclass(frozen=True)
class SubagentMaterialIdentity:
    """Stable identity for one material candidate against one Git base."""

    base_commit: str
    paths: tuple[str, ...]
    patch_sha256: str

    @classmethod
    def from_evidence(
        cls,
        *,
        base_commit: str,
        evidence: CandidateGitState,
    ) -> SubagentMaterialIdentity | None:
        paths = tuple(sorted(set(evidence.material_changed_paths)))
        patch_sha256 = str(evidence.patch_text_sha256 or "").strip().lower()
        if (
            not base_commit
            or not paths
            or not patch_sha256
            or not evidence.patch_text
            or not evidence.material_patch_complete
            or evidence.patch_capture_status != "complete"
            or evidence.omitted_material_paths
        ):
            return None
        return cls(
            base_commit=base_commit,
            paths=paths,
            patch_sha256=patch_sha256,
        )

    @property
    def sha256(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_commit": self.base_commit,
            "paths": list(self.paths),
            "patch_sha256": self.patch_sha256,
        }


class SubagentWorkspaceProvider:
    """Own isolated Git worktrees for one parent agent session."""

    def __init__(self, *, root: Path, store: SessionStore) -> None:
        self.root = root.resolve()
        self.store = store
        self.worktrees_root = store.session_artifact_root / "subagent_worktrees"
        self._records: dict[str, SubagentWorkspaceRecord] = {}
        self._canonical_by_identity: dict[SubagentMaterialIdentity, str] = {}
        self._lock = RLock()

    def _event(self, run_id: str, action: str, **fields: Any) -> None:
        self.store.append(
            "subagent_workspace",
            {"run_id": run_id, "action": action, **fields},
        )

    @staticmethod
    def _identity_fields(
        identity: SubagentMaterialIdentity | None,
    ) -> dict[str, Any]:
        if identity is None:
            return {}
        return {
            "material_identity": identity.to_dict(),
            "material_identity_sha256": identity.sha256,
        }

    @staticmethod
    def _needs_output_retention(record: SubagentWorkspaceRecord) -> bool:
        evidence = record.evidence
        return bool(
            record.state != "discarded"
            and evidence is not None
            and (
                evidence.retained_ignored_paths
                or evidence.omitted_material_paths
                or not evidence.material_patch_complete
                or evidence.patch_capture_status != "complete"
            )
        )

    @staticmethod
    def _refresh_workspace_evidence(record: SubagentWorkspaceRecord) -> None:
        """Never apply or clean up output that changed after the recorded capture."""
        if not record.worktree_path.exists():
            return
        current = capture_isolated_workspace_git_state(
            worktree_path=record.worktree_path, base_ref=record.base_commit
        )
        recorded = record.evidence
        if recorded is None:
            # A capture/artifact write may have failed before recording evidence.
            # Only a proven empty workspace is safe to clean up in that case.
            record.evidence = (
                replace(
                    current,
                    material_patch_complete=False,
                    patch_capture_status="partial",
                    reason_codes=(*current.reason_codes, "workspace_not_captured"),
                    working_tree_delta=None,
                )
                if current.patch_text or current.raw_changed_paths
                else current
            )
            return
        changed = (
            current.patch_text_sha256 != recorded.patch_text_sha256
            or current.material_changed_paths != recorded.material_changed_paths
        )
        if not current.material_patch_complete or changed:
            reasons = tuple(
                dict.fromkeys(
                    (
                        *recorded.reason_codes,
                        *current.reason_codes,
                        *(("workspace_changed_after_capture",) if changed else ()),
                    )
                )
            )
            record.evidence = replace(
                recorded,
                material_patch_complete=False,
                patch_capture_status="partial" if current.material_patch_complete else "failed",
                reason_codes=reasons,
                omitted_material_paths=tuple(
                    sorted(
                        set(
                            current.omitted_material_paths
                            or (*recorded.raw_changed_paths, *current.raw_changed_paths)
                        )
                    )
                ),
                retained_ignored_paths=current.retained_ignored_paths,
                working_tree_delta=None,
            )
            return
        record.evidence = replace(recorded, retained_ignored_paths=current.retained_ignored_paths)

    @staticmethod
    def _retained_output_fields(record: SubagentWorkspaceRecord) -> dict[str, Any]:
        evidence = record.evidence
        if evidence is None or not evidence.retained_ignored_paths:
            return {}
        return {
            "retained_ignored_paths": list(evidence.retained_ignored_paths),
            "ignored_outputs_integrated": False,
            "candidate_worktree_retained": record.worktree_path.exists(),
            "worktree_path": os.fspath(record.worktree_path),
            "retention_note": (
                "Git-ignored output remains in the worktree and is not included in the Git patch. "
                "Inspect it before explicitly discarding the retained workspace."
            ),
        }

    @staticmethod
    def _cleanup_status_fields(
        record: SubagentWorkspaceRecord,
        *related_records: SubagentWorkspaceRecord,
    ) -> dict[str, Any]:
        """Return cleanup state for an alias and any failed canonical cleanup."""

        tracked_records = [record]
        tracked_records.extend(
            candidate
            for candidate in related_records
            if candidate.cleanup_pending and candidate.run_id != record.run_id
        )
        pending_records = [candidate for candidate in tracked_records if candidate.cleanup_pending]
        fields: dict[str, Any] = {
            "cleanup_pending": bool(pending_records),
            "physical_worktree_removed": all(
                not candidate.worktree_path.exists() for candidate in tracked_records
            ),
        }
        if pending_records:
            fields["cleanup_pending_run_ids"] = [candidate.run_id for candidate in pending_records]
        warnings = list(
            dict.fromkeys(
                candidate.cleanup_error for candidate in pending_records if candidate.cleanup_error
            )
        )
        if warnings:
            fields["cleanup_warning"] = "\n".join(warnings)
        return fields

    def _retry_pending_cleanup_locked(
        self,
        record: SubagentWorkspaceRecord,
        *,
        release_action: str,
        retry: str,
    ) -> None:
        """Retry one previously failed cleanup after an explicit lifecycle action."""

        if not record.cleanup_pending:
            return
        cleanup_error = self._remove_worktree(record.worktree_path)
        if cleanup_error:
            record.cleanup_error = cleanup_error
            self._event(
                record.run_id,
                "cleanup_failed",
                release_action=release_action,
                error=cleanup_error,
                retry=retry,
            )
            return
        record.cleanup_pending = False
        record.cleanup_error = ""
        self._event(
            record.run_id,
            "cleanup_completed",
            release_action=release_action,
            retry=retry,
        )

    def _canonical_for_identity_locked(
        self,
        identity: SubagentMaterialIdentity,
        *,
        exclude_run_id: str,
    ) -> SubagentWorkspaceRecord | None:
        canonical_run_id = self._canonical_by_identity.get(identity)
        if not canonical_run_id or canonical_run_id == exclude_run_id:
            return None
        candidate = self._records.get(canonical_run_id)
        if candidate is not None and candidate.state == "captured":
            # Recheck the canonical's live output before discarding another
            # workspace in favor of its cached identity.
            self._refresh_workspace_evidence(candidate)
        if (
            candidate is None
            or candidate.state not in {"captured", "applied"}
            or candidate.material_identity != identity
            or self._needs_output_retention(candidate)
        ):
            self._canonical_by_identity.pop(identity, None)
            return None
        if candidate.state == "applied" and not self._patch_is_integrated_locked(candidate):
            self._canonical_by_identity.pop(identity, None)
            self._event(
                candidate.run_id,
                "integration_invalidated",
                **self._identity_fields(candidate.material_identity),
            )
            return None
        return candidate

    def _duplicate_target_locked(
        self,
        record: SubagentWorkspaceRecord,
    ) -> SubagentWorkspaceRecord | None:
        if not record.duplicate_of:
            return None
        return self._records.get(record.duplicate_of)

    def _patch_path(self, record: SubagentWorkspaceRecord) -> Path:
        return self.store.session_artifact_layout.artifact_fs_path(
            "subagent_patches",
            f"{record.run_id}.patch",
        )

    def _patch_is_integrated_locked(self, record: SubagentWorkspaceRecord) -> bool:
        if record.evidence is None or not record.evidence.patch_text:
            return False
        try:
            _run_git_checked(
                self.root,
                ["apply", "--reverse", "--check", os.fspath(self._patch_path(record))],
                error_message="isolated subagent patch is not integrated",
                disable_filters=True,
            )
        except GitOpsError:
            return False
        return True

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
            disable_filters=True,
        )
        if inside.stdout.strip().lower() != "true":
            raise GitOpsError("workspace_view=isolated requires a git repository")
        base_commit = _run_git_checked(
            self.root,
            ["rev-parse", "HEAD"],
            error_message="failed to resolve parent HEAD",
            disable_filters=True,
        ).stdout.strip()
        if not base_commit:
            raise GitOpsError("failed to resolve parent HEAD")
        try:
            status = _run_git_checked(
                self.root,
                ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                error_message="failed to inspect parent workspace",
                disable_filters=True,
            ).stdout
        except GitOpsError as exc:
            raise WorkspaceSnapshotError(
                "workspace_snapshot_git_failed",
                detail=f"{exc}. External Git filters are disabled for isolated workspaces",
            ) from exc
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
                parent_head_commit, parent_dirty_paths = self._parent_git_context()
            except WorkspaceSnapshotError as exc:
                return self._error(
                    run_id=normalized_run_id,
                    error_code=exc.reason,
                    message=str(exc),
                )
            except GitOpsError:
                return self._error(
                    run_id=normalized_run_id,
                    error_code="isolated_workspace_requires_git",
                    message="workspace_view=isolated requires a git repository",
                )

            excluded_paths: list[str] = []
            for artifact_path in (self.store.path, self.store.session_artifact_root):
                try:
                    relative = artifact_path.resolve().relative_to(self.root)
                except ValueError:
                    continue
                if relative.parts:
                    excluded_paths.append(relative.as_posix())
            try:
                base_commit = snapshot_workspace_baseline(
                    root=self.root,
                    parent_head=parent_head_commit,
                    excluded_paths=tuple(excluded_paths),
                )
            except WorkspaceSnapshotError as exc:
                return {
                    **self._error(
                        run_id=normalized_run_id,
                        error_code=exc.reason,
                        message=f"Could not snapshot the current parent workspace: {exc}.",
                    ),
                    "omitted_material_paths": list(exc.paths),
                }

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
                    [
                        "worktree",
                        "add",
                        "--detach",
                        "--no-checkout",
                        os.fspath(worktree_path),
                        base_commit,
                    ],
                    error_message="failed to create isolated subagent workspace",
                    disable_filters=True,
                )
                # Resolve filters in the child's own Git context: conditional
                # includes can select different drivers for this worktree.
                _run_git_checked(
                    worktree_path,
                    ["reset", "--hard", base_commit],
                    error_message=(
                        "failed to check out isolated subagent workspace "
                        "(external Git filters are disabled)"
                    ),
                    disable_filters=True,
                )
            except GitOpsError as exc:
                cleanup_error = self._remove_worktree(worktree_path)
                return self._error(
                    run_id=normalized_run_id,
                    error_code="workspace_prepare_failed",
                    message=str(exc)
                    + (f"; cleanup failed: {cleanup_error}" if cleanup_error else ""),
                )

            record = SubagentWorkspaceRecord(
                run_id=normalized_run_id,
                worktree_path=worktree_path,
                base_commit=base_commit,
                parent_dirty_paths=parent_dirty_paths,
                parent_head_commit=parent_head_commit,
            )
            self._records[normalized_run_id] = record
            self._event(
                normalized_run_id,
                "prepared",
                base_commit=base_commit,
                parent_head_commit=parent_head_commit,
                parent_dirty_paths=list(parent_dirty_paths),
            )
            return {
                "ok": True,
                "run_id": normalized_run_id,
                "worktree_path": os.fspath(worktree_path),
                "base_commit": base_commit,
                "parent_head_commit": parent_head_commit,
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
            if record.state in {"applied", "discarded", "duplicate"}:
                return self._error(
                    run_id=record.run_id,
                    error_code="workspace_already_released",
                    message=f"Isolated workspace {record.run_id} was already {record.state}.",
                )
            evidence = capture_isolated_workspace_git_state(
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
            no_changes = (
                not paths
                and not evidence.omitted_material_paths
                and not evidence.retained_ignored_paths
                and evidence.material_patch_complete
                and evidence.patch_capture_status == "complete"
            )
            record.no_changes = no_changes
            if (
                record.material_identity is not None
                and self._canonical_by_identity.get(record.material_identity) == record.run_id
            ):
                self._canonical_by_identity.pop(record.material_identity, None)
            material_identity = SubagentMaterialIdentity.from_evidence(
                base_commit=record.base_commit,
                evidence=evidence,
            )
            # Equal Git patches do not prove equal ignored output. Never remove
            # such worktrees through candidate deduplication.
            if evidence.retained_ignored_paths:
                material_identity = None
            record.material_identity = material_identity
            self._event(
                record.run_id,
                "captured",
                base_commit=record.base_commit,
                patch_artifact=patch_artifact,
                paths=paths,
                no_changes=no_changes,
                patch_capture_status=evidence.patch_capture_status,
                material_patch_complete=evidence.material_patch_complete,
                capture_reason_codes=list(evidence.reason_codes),
                omitted_material_paths=list(evidence.omitted_material_paths),
                **self._retained_output_fields(record),
                **self._identity_fields(material_identity),
            )
            result = {
                "ok": True,
                "run_id": record.run_id,
                "base_commit": record.base_commit,
                "parent_head_commit": record.parent_head_commit,
                "parent_dirty_paths": list(record.parent_dirty_paths),
                "patch_artifact": patch_artifact,
                "paths": paths,
                "insertions": _patch_line_count(evidence.patch_text, prefix="+"),
                "deletions": _patch_line_count(evidence.patch_text, prefix="-"),
                "sha256": evidence.patch_text_sha256,
                "patch_capture_status": evidence.patch_capture_status,
                "material_patch_complete": evidence.material_patch_complete,
                "capture_reason_codes": list(evidence.reason_codes),
                "omitted_material_paths": list(evidence.omitted_material_paths),
                "candidate_worktree_retained": record.worktree_path.exists(),
                "no_changes": no_changes,
                **self._identity_fields(material_identity),
                **self._retained_output_fields(record),
            }
            if no_changes:
                released = self.release(
                    record.run_id,
                    action="discarded",
                    reason="no_changes",
                )
                if not bool(released.get("ok")):
                    return released
                result.update(
                    {
                        "semantic_no_progress": True,
                        "candidate_worktree_retained": record.worktree_path.exists(),
                        **self._cleanup_status_fields(record),
                    }
                )
                return result

            canonical = (
                self._canonical_for_identity_locked(
                    material_identity,
                    exclude_run_id=record.run_id,
                )
                if material_identity is not None
                else None
            )
            if material_identity is not None and canonical is None:
                self._canonical_by_identity[material_identity] = record.run_id
            if canonical is not None:
                record.state = "duplicate"
                record.duplicate_of = canonical.run_id
                record.already_integrated = canonical.state == "applied"
                cleanup_error = self._remove_worktree(record.worktree_path)
                if cleanup_error:
                    record.cleanup_pending = True
                    record.cleanup_error = cleanup_error
                duplicate_fields = {
                    "duplicate_of": canonical.run_id,
                    "canonical_run_id": canonical.run_id,
                    "canonical_state": canonical.state,
                    "already_integrated": record.already_integrated,
                    "semantic_no_progress": True,
                    # A duplicate is not a second logical candidate, but a
                    # failed cleanup still leaves its physical worktree
                    # retained. Keep this field aligned with the observable
                    # filesystem state instead of claiming cleanup succeeded.
                    "candidate_worktree_retained": record.worktree_path.exists(),
                    **self._identity_fields(material_identity),
                    **self._cleanup_status_fields(record, canonical),
                }
                self._event(
                    record.run_id,
                    "duplicate",
                    base_commit=record.base_commit,
                    patch_artifact=patch_artifact,
                    paths=paths,
                    **duplicate_fields,
                )
                result.update(duplicate_fields)
                if cleanup_error:
                    self._event(
                        record.run_id,
                        "cleanup_failed",
                        release_action="duplicate",
                        error=cleanup_error,
                    )
                    result.update(
                        {
                            "cleanup_pending": True,
                            "cleanup_warning": cleanup_error,
                        }
                    )
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
            if record.state == "duplicate":
                canonical = self._duplicate_target_locked(record)
                return {
                    **self._error(
                        run_id=source,
                        error_code="duplicate_workspace_use_canonical",
                        message=(
                            f"Isolated workspace {source} duplicates "
                            f"{record.duplicate_of}; resume from the canonical run instead."
                        ),
                    ),
                    "duplicate_of": record.duplicate_of,
                    "canonical_run_id": record.duplicate_of,
                    "semantic_no_progress": True,
                    **self._identity_fields(record.material_identity),
                    **self._cleanup_status_fields(
                        record,
                        *((canonical,) if canonical is not None else ()),
                    ),
                }
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
            duplicates = [
                duplicate
                for duplicate in self._records.values()
                if duplicate.duplicate_of == source
            ]
            # Persist before transferring ownership. An append failure must
            # leave the original run and all aliases available for retry.
            for duplicate in duplicates:
                self._event(
                    duplicate.run_id,
                    "duplicate_retargeted",
                    previous_canonical_run_id=source,
                    canonical_run_id=target,
                    **self._identity_fields(duplicate.material_identity),
                )
            self._event(
                target,
                "reattached",
                resumed_from=source,
                base_commit=record.base_commit,
                patch_artifact=record.patch_artifact or None,
            )
            self._records.pop(source)
            record.run_id = target
            self._records[target] = record
            if (
                record.material_identity is not None
                and self._canonical_by_identity.get(record.material_identity) == source
            ):
                self._canonical_by_identity[record.material_identity] = target
            for duplicate in duplicates:
                duplicate.duplicate_of = target
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
        requested_run_id = run_id
        duplicate_of = ""
        duplicate_record: SubagentWorkspaceRecord | None = None
        if record.state == "duplicate":
            duplicate_record = record
            duplicate_of = record.duplicate_of
            canonical = self._duplicate_target_locked(record)
            if canonical is None:
                return {
                    **self._error(
                        run_id=record.run_id,
                        error_code="duplicate_workspace_canonical_missing",
                        message=(
                            f"Canonical isolated workspace {record.duplicate_of} for "
                            f"duplicate run {record.run_id} is unavailable."
                        ),
                    ),
                    "duplicate_of": record.duplicate_of,
                    "canonical_run_id": record.duplicate_of,
                    "semantic_no_progress": True,
                    **self._cleanup_status_fields(record),
                }
            record = canonical
        if record.state in {"applied", "discarded"}:
            return self._error(
                run_id=requested_run_id,
                error_code="workspace_from_run_released",
                message=f"Isolated workspace {record.run_id} was already {record.state}.",
            )
        if record.state != "captured" or record.evidence is None:
            return self._error(
                run_id=requested_run_id,
                error_code="workspace_from_run_not_ready",
                message=f"Isolated workspace {record.run_id} is not a completed result.",
            )
        return {
            "ok": True,
            "run_id": record.run_id,
            **({"requested_run_id": requested_run_id} if duplicate_of else {}),
            **({"duplicate_of": duplicate_of} if duplicate_of else {}),
            **({"canonical_run_id": record.run_id} if duplicate_of else {}),
            **(
                self._cleanup_status_fields(duplicate_record, record)
                if duplicate_record is not None
                else {}
            ),
            "worktree_path": os.fspath(record.worktree_path),
            "base_commit": record.base_commit,
            "paths": list(record.evidence.material_changed_paths),
            **self._identity_fields(record.material_identity),
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
            if record.state == "duplicate":
                canonical = self._duplicate_target_locked(record)
                if canonical is None:
                    return
                record = canonical
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

    def _patch_summary(self, record: SubagentWorkspaceRecord) -> dict[str, Any]:
        evidence = record.evidence
        patch_text = evidence.patch_text if evidence is not None else ""
        return {
            "files": (list(evidence.material_changed_paths) if evidence is not None else []),
            "insertions": _patch_line_count(patch_text, prefix="+"),
            "deletions": _patch_line_count(patch_text, prefix="-"),
            "patch_artifact": record.patch_artifact,
            "sha256": evidence.patch_text_sha256 if evidence is not None else "",
            **self._retained_output_fields(record),
        }

    def _apply_duplicate_locked(
        self,
        record: SubagentWorkspaceRecord,
    ) -> dict[str, Any]:
        canonical = self._duplicate_target_locked(record)
        common = {
            "run_id": record.run_id,
            "duplicate_of": record.duplicate_of,
            "canonical_run_id": record.duplicate_of,
            **self._identity_fields(record.material_identity),
        }
        if canonical is None:
            return {
                **self._error(
                    run_id=record.run_id,
                    error_code="duplicate_workspace_canonical_missing",
                    message=(
                        f"Canonical isolated workspace {record.duplicate_of} for "
                        f"duplicate run {record.run_id} is unavailable."
                    ),
                ),
                **common,
                **self._cleanup_status_fields(record),
                "semantic_no_progress": True,
            }
        common.update(self._cleanup_status_fields(record, canonical))
        common.update(self._retained_output_fields(canonical))
        if canonical.state == "applied":
            if not self._patch_is_integrated_locked(canonical):
                record.already_integrated = False
                return {
                    **self._error(
                        run_id=record.run_id,
                        error_code="already_integrated_state_changed",
                        message=(
                            f"The parent workspace no longer contains the material result "
                            f"from canonical run {canonical.run_id}; create a fresh candidate."
                        ),
                    ),
                    **common,
                    "canonical_state": canonical.state,
                    "semantic_no_progress": False,
                }
            record.already_integrated = True
            self._event(
                record.run_id,
                "already_integrated",
                canonical_run_id=canonical.run_id,
                paths=list(record.material_identity.paths) if record.material_identity else [],
                **self._identity_fields(record.material_identity),
            )
            return {
                "ok": True,
                **common,
                "action": "already_integrated",
                "already_integrated": True,
                "semantic_no_progress": True,
                "applied_paths": [],
                "integrated_paths": (
                    list(record.material_identity.paths) if record.material_identity else []
                ),
                "patch_summary": self._patch_summary(record),
            }
        if canonical.state != "captured":
            return {
                **self._error(
                    run_id=record.run_id,
                    error_code="duplicate_candidate_released",
                    message=(
                        f"Canonical isolated workspace {canonical.run_id} was already "
                        f"{canonical.state}."
                    ),
                ),
                **common,
                "canonical_state": canonical.state,
                "semantic_no_progress": True,
            }

        applied = self.apply(canonical.run_id)
        if not bool(applied.get("ok")):
            return {
                **applied,
                **common,
                "canonical_error": applied.get("error"),
            }
        self._event(
            record.run_id,
            "duplicate_applied",
            canonical_run_id=canonical.run_id,
            paths=list(applied.get("applied_paths") or []),
            **self._identity_fields(record.material_identity),
        )
        return {
            **applied,
            **common,
            **self._cleanup_status_fields(record, canonical),
            "applied_via_canonical": True,
            "semantic_no_progress": False,
        }

    def apply(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(str(run_id or "").strip())
            if record is None:
                return self._error(
                    run_id=str(run_id or ""),
                    error_code="unknown_workspace_run",
                    message=f"Unknown isolated workspace run: {run_id}",
                )
            if record.state == "duplicate":
                return self._apply_duplicate_locked(record)
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
            self._refresh_workspace_evidence(record)
            evidence = record.evidence
            paths = list(evidence.material_changed_paths)
            if (
                not evidence.material_patch_complete
                or evidence.patch_capture_status != "complete"
                or evidence.omitted_material_paths
            ):
                return {
                    **self._error(
                        run_id=record.run_id,
                        error_code=(
                            "workspace_changed_after_capture"
                            if "workspace_changed_after_capture" in evidence.reason_codes
                            else "incomplete_workspace_patch"
                        ),
                        message=(
                            f"Isolated workspace {record.run_id} needs a complete, current capture "
                            "before applying. Resume the child to inspect and recapture its output."
                        ),
                    ),
                    "capture_reason_codes": list(evidence.reason_codes),
                    "omitted_material_paths": list(evidence.omitted_material_paths),
                    "candidate_worktree_retained": record.worktree_path.exists(),
                }
            if not paths or not evidence.patch_text:
                return {
                    **self._error(
                        run_id=record.run_id,
                        error_code="no_captured_changes",
                        message=f"Isolated workspace {record.run_id} has no Git patch to apply.",
                    ),
                    **self._retained_output_fields(record),
                }
            patch_path = self._patch_path(record)
            try:
                _run_git_checked(
                    self.root,
                    ["apply", "--check", os.fspath(patch_path)],
                    error_message="isolated subagent patch conflicts with the parent workspace",
                    disable_filters=True,
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
                    disable_filters=True,
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

            summary = self._patch_summary(record)
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
                **self._retained_output_fields(record),
            )
            result: dict[str, Any] = {
                "ok": True,
                "run_id": record.run_id,
                "applied_paths": paths,
                "patch_summary": summary,
                **self._retained_output_fields(record),
            }
            if self._needs_output_retention(record):
                return result
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
            if record.state == "duplicate":
                self._retry_pending_cleanup_locked(
                    record,
                    release_action="duplicate",
                    retry="explicit_discard",
                )
                canonical = self._duplicate_target_locked(record)
                # Releasing a duplicate consumes only the alias.  Its canonical
                # candidate stays available, while this run id becomes terminal
                # just like any other explicitly discarded workspace.
                record.state = "discarded"
                self._event(
                    record.run_id,
                    "duplicate_discarded",
                    canonical_run_id=record.duplicate_of,
                    canonical_state=canonical.state if canonical is not None else "missing",
                    **self._identity_fields(record.material_identity),
                )
                return {
                    "ok": True,
                    "run_id": record.run_id,
                    "action": action,
                    "duplicate_of": record.duplicate_of,
                    "canonical_run_id": record.duplicate_of,
                    "canonical_state": canonical.state if canonical is not None else "missing",
                    "semantic_no_progress": True,
                    "paths": (
                        list(record.material_identity.paths)
                        if record.material_identity is not None
                        else []
                    ),
                    "patch_artifact": record.patch_artifact or None,
                    **self._identity_fields(record.material_identity),
                    **self._cleanup_status_fields(
                        record,
                        *((canonical,) if canonical is not None else ()),
                    ),
                }
            if record.state == "discarded" or (
                record.state == "applied"
                and not (action == "discarded" and self._needs_output_retention(record))
            ):
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
            record.cleanup_pending = False
            record.cleanup_error = ""
            if (
                record.material_identity is not None
                and self._canonical_by_identity.get(record.material_identity) == record.run_id
            ):
                self._canonical_by_identity.pop(record.material_identity, None)
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
                disable_filters=True,
            )
            return ""
        except GitOpsError as exc:
            try:
                _cleanup_workspace_path(worktree_path)
                _run_git_checked(
                    self.root,
                    ["worktree", "prune"],
                    error_message="failed to prune isolated subagent workspace metadata",
                    disable_filters=True,
                )
            except GitOpsError:
                return str(exc)
            return ""

    def close(self) -> dict[str, Any]:
        with self._lock:
            for record in self._records.values():
                record.pinned_by_run_ids = ()
                if record.state != "discarded":
                    self._refresh_workspace_evidence(record)
            unresolved = [
                run_id
                for run_id, record in self._records.items()
                if record.state not in {"applied", "discarded", "duplicate"}
                and not self._needs_output_retention(record)
            ]
            cleanup_pending = [
                run_id
                for run_id, record in self._records.items()
                if record.cleanup_pending
                and run_id not in unresolved
                and not self._needs_output_retention(record)
            ]
        released_run_ids: list[str] = []
        cleanup_completed_run_ids: list[str] = []
        failures: list[dict[str, str]] = []
        for run_id in unresolved:
            released = self.release(run_id, action="discarded")
            if bool(released.get("ok")):
                released_run_ids.append(run_id)
                continue
            error = str(released.get("error") or "failed to release isolated workspace")
            error_code = str(released.get("error_code") or "workspace_release_failed")
            with self._lock:
                record = self._records.get(run_id)
                if record is not None:
                    record.cleanup_pending = True
                    record.cleanup_error = error
                    self._event(
                        record.run_id,
                        "cleanup_failed",
                        release_action="discarded",
                        error=error,
                        error_code=error_code,
                        retry="session_close",
                    )
            failures.append(
                {
                    "run_id": run_id,
                    "error": error,
                    "error_code": error_code,
                }
            )
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
                    failures.append(
                        {
                            "run_id": record.run_id,
                            "error": cleanup_error,
                            "error_code": "workspace_release_failed",
                        }
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
                cleanup_completed_run_ids.append(record.run_id)
        with self._lock:
            still_pending = sorted(
                record.run_id for record in self._records.values() if record.cleanup_pending
            )
        summary = {
            "ok": not failures and not still_pending,
            "released_run_ids": sorted(released_run_ids),
            "cleanup_completed_run_ids": sorted(cleanup_completed_run_ids),
            "cleanup_pending_run_ids": still_pending,
            "failures": failures,
        }
        with self._lock:
            retained = [
                {
                    "run_id": record.run_id,
                    "worktree_path": os.fspath(record.worktree_path),
                    "material_patch_complete": record.evidence.material_patch_complete,
                    **self._retained_output_fields(record),
                }
                for record in self._records.values()
                if self._needs_output_retention(record) and record.worktree_path.exists()
            ]
        if retained:
            summary["retained_output_workspaces"] = retained
        self.store.append("subagent_workspace_cleanup_summary", summary)
        return summary


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
