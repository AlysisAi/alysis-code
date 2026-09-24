"""Task-owned preservation authority and monotonic workspace evidence generations.

Verification results stay turn-local. Only the original workspace baseline and
observed edits cross turns: restarting a turn must never legitimize a rewritten
checker, erase a preservation violation, or recycle a checkpoint generation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..verification_command_analysis import CheckerEntrypointFingerprint
from .acceptance_contract import (
    AcceptanceContract,
    AcceptanceWorkspaceSnapshot,
    capture_acceptance_workspace_snapshot,
)

TASK_EVIDENCE_EVENT = "session_task_evidence"


@dataclass
class TaskEvidenceState:
    task_id: str
    workspace_root: str
    snapshot: AcceptanceWorkspaceSnapshot
    baseline_available: bool = True
    material_edit_count: int = 0
    material_edit_generation: int = 0
    verification_relevant_edit_generation: int = 0
    touched_repo_paths: set[str] = field(default_factory=set)

    def to_payload(self) -> dict[str, Any]:
        # AcceptanceWorkspaceSnapshot.as_payload is a truncated diagnostic view;
        # durable authority must retain every captured entry.
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "workspace_root": self.workspace_root,
            "baseline_available": self.baseline_available,
            "material_edit_count": self.material_edit_count,
            "material_edit_generation": self.material_edit_generation,
            "verification_relevant_edit_generation": self.verification_relevant_edit_generation,
            "touched_repo_paths": sorted(self.touched_repo_paths),
            "snapshot": {
                "preexisting_paths": sorted(self.snapshot.preexisting_paths),
                "preexisting_test_paths": sorted(self.snapshot.preexisting_test_paths),
                "preexisting_checker_paths": sorted(self.snapshot.preexisting_checker_paths),
                "preexisting_checker_fingerprints": [
                    value.as_payload() for value in self.snapshot.preexisting_checker_fingerprints
                ],
                "preexisting_verify_commands": list(self.snapshot.preexisting_verify_commands),
            },
        }


def acceptance_revision(contract: AcceptanceContract, task: Any) -> str:
    """Bind candidates to accepted requirements and host-selected obligations."""
    payload = {
        "task_id": contract.task_id,
        "objective": getattr(task, "objective", ""),
        "amendments": list(getattr(task, "amendments", ())),
        "criteria": [
            {
                key: value
                for key, value in criterion.as_payload().items()
                if key not in {"status", "failure_summary", "evidence_ids", "service_ids"}
            }
            for criterion in contract.criteria
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def begin_task_evidence(
    session: Any, *, new_task: bool, repo_scan: Any = None
) -> TaskEvidenceState:
    current = getattr(session, "_task_evidence_state", None)
    task_id = getattr(session.task_state, "task_id", "")
    if new_task or current is None or current.task_id != task_id:
        current = TaskEvidenceState(
            task_id=task_id,
            workspace_root=str(session.root.resolve()),
            snapshot=capture_acceptance_workspace_snapshot(
                root=session.root,
                repo_scan=repo_scan,
                authoritative_verification_commands=session.authoritative_verification_commands,
                effective_verification_commands=list(session.effective_verification_commands),
            )
            if new_task
            else AcceptanceWorkspaceSnapshot(),
            baseline_available=new_task,
        )
        session._task_evidence_state = current
        persist_task_evidence(session)
    return current


def install_task_evidence(session: Any, contract: AcceptanceContract) -> TaskEvidenceState:
    current = begin_task_evidence(session, new_task=False)
    contract.snapshot = current.snapshot
    contract.baseline_available = current.baseline_available
    contract.generation = current.verification_relevant_edit_generation
    contract.acceptance_revision = acceptance_revision(contract, session.task_state)
    return current


def seed_execution_state(state: Any, baseline: TaskEvidenceState) -> None:
    state.material_edit_count = baseline.material_edit_count
    state.material_edit_generation = baseline.material_edit_generation
    state.verification_relevant_edit_generation = baseline.verification_relevant_edit_generation
    state.touched_repo_paths.update(baseline.touched_repo_paths)


def persist_task_evidence(session: Any, execution_state: Any = None) -> None:
    baseline = getattr(session, "_task_evidence_state", None)
    if baseline is None:
        return
    if execution_state is not None:
        for name in (
            "material_edit_count",
            "material_edit_generation",
            "verification_relevant_edit_generation",
        ):
            setattr(baseline, name, max(getattr(baseline, name), getattr(execution_state, name)))
        baseline.touched_repo_paths.update(execution_state.touched_repo_paths)
    try:
        session.store.append(TASK_EVIDENCE_EVENT, baseline.to_payload())
    except Exception:
        # Continue safely in memory, but neither this turn nor a resume may claim
        # original authority after losing the durable edit ledger.
        baseline.baseline_available = False
        if execution_state is not None and execution_state.acceptance_contract is not None:
            execution_state.acceptance_contract.baseline_available = False


def restore_task_evidence(
    session: Any, payload: Any, *, event_session_id: str | None, expected_session_id: str | None
) -> None:
    session._task_evidence_state = None
    task_id = getattr(session.task_state, "task_id", "")
    if not task_id or getattr(session, "root", None) is None:
        return
    try:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("missing task evidence baseline")
        if (
            payload.get("task_id") != task_id
            or payload.get("workspace_root") != str(session.root.resolve())
            or not event_session_id
            or (expected_session_id and event_session_id != expected_session_id)
        ):
            raise ValueError("task evidence identity mismatch")
        snapshot = payload["snapshot"]

        def strings(source: dict[str, Any], key: str) -> list[str]:
            value = source[key]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("invalid baseline paths")
            return value

        fingerprints = []
        for value in snapshot["preexisting_checker_fingerprints"]:
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("display_path"), str)
                or not isinstance(value.get("resolved_path"), str)
                or not isinstance(value.get("is_regular_file"), bool)
                or (
                    value.get("size") is not None
                    and (type(value["size"]) is not int or value["size"] < 0)
                )
                or (
                    value.get("sha256") is not None
                    and (not isinstance(value["sha256"], str) or len(value["sha256"]) != 64)
                )
            ):
                raise ValueError("invalid checker fingerprint")
            fingerprints.append(CheckerEntrypointFingerprint(**value))
        restored = TaskEvidenceState(
            task_id=task_id,
            workspace_root=str(session.root.resolve()),
            snapshot=AcceptanceWorkspaceSnapshot(
                preexisting_paths=frozenset(strings(snapshot, "preexisting_paths")),
                preexisting_test_paths=frozenset(strings(snapshot, "preexisting_test_paths")),
                preexisting_checker_paths=frozenset(strings(snapshot, "preexisting_checker_paths")),
                preexisting_checker_fingerprints=tuple(fingerprints),
                preexisting_verify_commands=tuple(strings(snapshot, "preexisting_verify_commands")),
            ),
            baseline_available=payload["baseline_available"] is True,
            touched_repo_paths=set(strings(payload, "touched_repo_paths")),
        )
        for name in (
            "material_edit_count",
            "material_edit_generation",
            "verification_relevant_edit_generation",
        ):
            value = payload[name]
            if type(value) is not int or value < 0:
                raise ValueError("invalid task evidence generation")
            setattr(restored, name, value)
        session._task_evidence_state = restored
    except (KeyError, TypeError, ValueError):
        session._task_evidence_state = TaskEvidenceState(
            task_id=task_id,
            workspace_root=str(session.root.resolve()),
            snapshot=AcceptanceWorkspaceSnapshot(),
            baseline_available=False,
        )
    # A resumed log must carry the baseline onward, including an explicit
    # unavailable marker, so another resume cannot silently recreate it.
    persist_task_evidence(session)
